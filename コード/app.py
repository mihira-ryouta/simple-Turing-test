"""
人工知能判別ゲーム ― 最小プロトタイプ (非対称型1対1 × 役割質問式)
＋ データベース保存・アンケート対応版
＋ オンライン最小実装 (クイックマッチ / 人対人2人)

構成:
  - シングル(従来): あなた(質問者) vs AI(回答者)。HTTPルート(/start /ask /judge ...)。
  - オンライン(新規): クイックマッチで人間2人を接続。質問者/回答者をランダム割当。
    通信は Socket.IO (Flask-SocketIO)。今回は必ず人間2人で、AI補填は未実装(ステップ2)。

起動方法:
  pip install flask flask-socketio google-generativeai
  python app.py
  ブラウザで http://127.0.0.1:5000 を開く
  (オンラインの動作確認は2つのタブ/端末で「グループ→公開」を押す)
"""

from flask import Flask, render_template, request, jsonify, session
from flask_socketio import SocketIO, emit
import ai_backend
import database as db
import random
import threading
import uuid

app = Flask(__name__)
app.secret_key = "dev-secret-change-me"  # 本番では必ず変更すること

# Socket.IO を初期化。ローカル開発では threading モードが最も導入が簡単
# (eventlet/gevent を別途入れなくても動く)。将来 本番化するなら要検討。
socketio = SocketIO(app, async_mode="threading", cors_allowed_origins="*")

# 1ゲームの質問回数(これを過ぎたら判定フェーズへ)
MAX_TURNS = 5

# 使用中のAIモデル名(記録用)。ai_backend側と揃えておく。
AI_MODEL_NAME = "gemini-1.5-flash"

# プレイヤー識別子(1対1なので固定)。大人数化したら動的に振る。
HUMAN_KEY = "p_human"
OPPONENT_KEY = "p_opponent"

# アプリ起動時にDBを用意
db.init_db()


@app.route("/")
def index():
    return render_template("index.html")


# ==========================================================================
#  シングル(従来のHTTP実装) ― 変更なし
# ==========================================================================

@app.route("/start", methods=["POST"])
def start():
    """新しいゲームを開始。相手がAIか人間かをランダムに決める(今は常にAI)。"""
    session["game_id"] = "game_" + uuid.uuid4().hex[:12]
    session["opponent"] = "ai"
    session["history"] = []
    session["turn"] = 0
    session["saved"] = False  # 二重保存を防ぐフラグ
    return jsonify({"status": "started", "max_turns": MAX_TURNS})


@app.route("/ask", methods=["POST"])
def ask():
    """質問者(あなた)の質問を受け取り、回答者(AI)の答えを返す。"""
    question = (request.json or {}).get("question", "").strip()
    if not question:
        return jsonify({"error": "質問が空です"}), 400

    history = session.get("history", [])
    turn = session.get("turn", 0)

    if turn >= MAX_TURNS:
        return jsonify({"error": "質問回数の上限です。判定してください。"}), 400

    history.append({"role": "questioner", "text": question})
    reply = ai_backend.generate_reply(history)
    history.append({"role": "ai", "text": reply})

    turn += 1
    session["history"] = history
    session["turn"] = turn

    return jsonify({
        "reply": reply,
        "turn": turn,
        "max_turns": MAX_TURNS,
        "can_judge": turn >= MAX_TURNS,
    })


@app.route("/judge", methods=["POST"])
def judge():
    """あなたの判定(human/ai)を受け取り、正解と照合し、ゲームをDBに保存する。"""
    guess = (request.json or {}).get("guess", "")
    answer = session.get("opponent", "ai")
    correct = (guess == answer)

    game_id = session.get("game_id")
    if game_id and not session.get("saved"):
        result = "human_win" if correct else "ai_win"
        db.save_game(
            game_id=game_id, entry_type="single", player_count=2,
            ai_present=(answer == "ai"),
            ai_count=1 if answer == "ai" else 0,
            ai_model=AI_MODEL_NAME if answer == "ai" else None,
            result=result,
        )
        db.save_player(game_id, HUMAN_KEY, role="human",
                       vote_target=OPPONENT_KEY, correct=correct)
        db.save_player(game_id, OPPONENT_KEY, role=answer,
                       vote_target=None, correct=None)

        history = session.get("history", [])
        turn = 0
        last_question_id = None
        for h in history:
            if h["role"] == "questioner":
                turn += 1
                last_question_id = db.save_message(
                    game_id, turn, HUMAN_KEY, "question", h["text"])
            else:
                db.save_message(
                    game_id, turn, OPPONENT_KEY, "answer", h["text"],
                    reply_to=last_question_id)
        session["saved"] = True

    return jsonify({
        "correct": correct, "answer": answer,
        "your_guess": guess, "game_id": game_id,
    })


@app.route("/survey", methods=["POST"])
def survey():
    """任意アンケートを保存する。スキップされたら呼ばれない。"""
    game_id = session.get("game_id")
    if not game_id:
        return jsonify({"error": "ゲームがありません"}), 400
    data = request.json or {}
    db.save_survey(
        game_id=game_id, respondent_id=HUMAN_KEY,
        confidence=data.get("confidence"),
        basis=data.get("basis", ""),
        free_text=data.get("free_text", ""),
    )
    return jsonify({"status": "saved"})


@app.route("/messages", methods=["GET"])
def get_messages():
    """今のゲームの会話ログを返す (注釈画面で「どの発言か」を選ぶ材料)。"""
    game_id = session.get("game_id")
    if not game_id:
        return jsonify({"messages": []})
    import database as _db
    with _db._conn() as conn:
        rows = conn.execute(
            """SELECT id, turn, speaker_id, kind, content
               FROM messages WHERE game_id=? ORDER BY id""",
            (game_id,),
        ).fetchall()
    return jsonify({"messages": [dict(r) for r in rows]})


@app.route("/annotate", methods=["POST"])
def annotate():
    """特定の発言への注釈を保存する (決め手/違和感 など)。任意。"""
    game_id = session.get("game_id")
    if not game_id:
        return jsonify({"error": "ゲームがありません"}), 400
    data = request.json or {}
    message_id = data.get("message_id")
    if message_id is None:
        return jsonify({"error": "message_idが必要です"}), 400
    db.save_annotation(
        game_id=game_id, message_id=message_id, annotator_id=HUMAN_KEY,
        kind=data.get("kind", ""), free_text=data.get("free_text", ""),
    )
    return jsonify({"status": "saved"})


# ==========================================================================
#  オンライン(クイックマッチ / 人対人2人) ― 新規
#
#  設計メモ:
#   - 状態は「サーバー側」にサーバー全体で持つ (session はクライアント毎なので
#     2人で1ゲームを共有するオンラインには使えない)。今はプロセス内メモリの dict。
#   - 役割は非対称型: 質問者(asker) / 回答者(answerer) をランダムに割り当てる。
#   - 今回は必ず人間2人。相手の正体は常に "human"。
#   - AI補填(空き巣ならAIで埋める)と、待ち時間・AI/人の優先度ルールは
#     ステップ2で本モジュールの _on_join_quick に追加する予定。
#   - プロセス内メモリなので、ワーカーを複数持つと共有できない。
#     複数ワーカー化する場合は Redis 等のメッセージキューが必要
#     (出典: Flask-SocketIO ドキュメント参照)。今は単一プロセスで運用。
# ==========================================================================

QUICK_ENTRY = "quick"
ASKER_KEY = "p_asker"
ANSWERER_KEY = "p_answerer"

_waiting = []      # クイックマッチ待機中の sid のリスト
_rooms = {}        # room_id -> 部屋の状態 dict
_sid_room = {}     # sid -> room_id (逆引き)
_lock = threading.Lock()  # _waiting/_rooms を触るときの排他


def _make_room(sid_a, sid_b):
    """2人ぶんの sid から部屋を作り、役割をランダムに割り当てる。"""
    room_id = "room_" + uuid.uuid4().hex[:8]
    asker, answerer = random.sample([sid_a, sid_b], 2)  # 順番をランダム化
    _rooms[room_id] = {
        "game_id": "game_" + uuid.uuid4().hex[:12],
        "asker": asker,
        "answerer": answerer,
        "turn": 0,
        "history": [],   # {"role": "questioner"/"answerer", "text": ...}
        "saved": False,
    }
    _sid_room[sid_a] = room_id
    _sid_room[sid_b] = room_id
    return room_id


def _save_online_game(room, guess, answer, correct):
    """人対人ゲームをDBに保存する。シングルの judge() と同じ構造。"""
    if room.get("saved"):
        return
    game_id = room["game_id"]
    result = "human_correct" if correct else "human_wrong"
    db.save_game(
        game_id=game_id, entry_type=QUICK_ENTRY, player_count=2,
        ai_present=False, ai_count=0, ai_model=None, result=result,
    )
    # 質問者(判定した側) と 回答者(判定された側)
    db.save_player(game_id, ASKER_KEY, role="human",
                   vote_target=ANSWERER_KEY, correct=correct)
    db.save_player(game_id, ANSWERER_KEY, role="human",
                   vote_target=None, correct=None)
    # 会話ログ。質問→回答を reply_to で紐付ける。
    turn = 0
    last_qid = None
    for h in room["history"]:
        if h["role"] == "questioner":
            turn += 1
            last_qid = db.save_message(
                game_id, turn, ASKER_KEY, "question", h["text"])
        else:
            db.save_message(
                game_id, turn, ANSWERER_KEY, "answer", h["text"],
                reply_to=last_qid)
    room["saved"] = True


@socketio.on("connect")
def _on_connect(auth=None):
    # 接続しただけでは何もしない。マッチングは join_quick で明示的に行う。
    pass


@socketio.on("join_quick")
def _on_join_quick():
    """クイックマッチ待機列に入る。相手がいれば即マッチ、いなければ待機。"""
    sid = request.sid
    partner = None
    with _lock:
        # 二重登録の除去(再送対策)
        _waiting[:] = [s for s in _waiting if s != sid]
        if _waiting:
            partner = _waiting.pop(0)
        else:
            _waiting.append(sid)

    if partner is None:
        emit("waiting", {"msg": "対戦相手を探しています…"})
        return

    # マッチ成立 → 部屋を作って両者に役割を通知
    with _lock:
        room_id = _make_room(partner, sid)
    room = _rooms[room_id]
    socketio.emit("match_found",
                  {"role": "asker", "max_turns": MAX_TURNS},
                  to=room["asker"])
    socketio.emit("match_found",
                  {"role": "answerer", "max_turns": MAX_TURNS},
                  to=room["answerer"])


@socketio.on("submit_question")
def _on_question(data):
    """質問者の質問を受け取り、回答者へ転送する。"""
    sid = request.sid
    room = _rooms.get(_sid_room.get(sid))
    if not room or sid != room["asker"]:
        return
    if room["turn"] >= MAX_TURNS:
        return
    text = (data or {}).get("text", "").strip()
    if not text:
        return
    room["history"].append({"role": "questioner", "text": text})
    socketio.emit("question",
                  {"text": text, "turn": room["turn"] + 1, "max_turns": MAX_TURNS},
                  to=room["answerer"])


@socketio.on("submit_answer")
def _on_answer(data):
    """回答者の回答を受け取り、質問者へ転送する。ターンを1進める。"""
    sid = request.sid
    room = _rooms.get(_sid_room.get(sid))
    if not room or sid != room["answerer"]:
        return
    text = (data or {}).get("text", "").strip()
    if not text:
        return
    room["history"].append({"role": "answerer", "text": text})
    room["turn"] += 1
    can_judge = room["turn"] >= MAX_TURNS
    socketio.emit("answer",
                  {"text": text, "turn": room["turn"],
                   "max_turns": MAX_TURNS, "can_judge": can_judge},
                  to=room["asker"])
    socketio.emit("turn_update",
                  {"turn": room["turn"], "max_turns": MAX_TURNS},
                  to=room["answerer"])


@socketio.on("submit_judge")
def _on_judge(data):
    """質問者の判定を受け取り、保存し、両者に結果を返す。"""
    sid = request.sid
    room = _rooms.get(_sid_room.get(sid))
    if not room or sid != room["asker"]:
        return
    guess = (data or {}).get("guess", "")
    answer = "human"  # クイックの人対人なので相手は常に人間
    correct = (guess == answer)
    _save_online_game(room, guess, answer, correct)

    payload = {"correct": correct, "answer": answer,
               "your_guess": guess, "game_id": room["game_id"]}
    socketio.emit("result", {**payload, "you_are": "asker"}, to=room["asker"])
    socketio.emit("result", {**payload, "you_are": "answerer"}, to=room["answerer"])


@socketio.on("disconnect")
def _on_disconnect(*args):
    """切断時のクリーンアップ。待機列/部屋から除去し、相手に通知する。"""
    sid = request.sid
    with _lock:
        if sid in _waiting:
            _waiting.remove(sid)
    room_id = _sid_room.pop(sid, None)
    if room_id and room_id in _rooms:
        room = _rooms[room_id]
        other = room["answerer"] if sid == room["asker"] else room["asker"]
        _sid_room.pop(other, None)
        socketio.emit("opponent_left", {}, to=other)
        _rooms.pop(room_id, None)


if __name__ == "__main__":
    # 従来の app.run ではなく socketio.run で起動する
    socketio.run(app, debug=True, port=5000)
