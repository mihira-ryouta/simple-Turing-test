# -*- coding: utf-8 -*-
"""
人工知能判別ゲーム ― アップグレード版 (非対称型1対1 × 役割質問式)

このバージョンで実装された設計決定 (決定メモ「AI人優先度ルール_決定メモ.md」対応):
  D2  α=0.4        … クイックの正体ドロー確率 (AI=0.4 / 人間=0.6)
  D4  W事前ドロー   … キュー投入時にAI切替デッドラインWを引く。W到達までは
                      人間マッチを探索、未マッチならAI戦へ (人間ドローなら backfilled=1)
  D5  b1           … AI応答に遅延を挿入 (delay_model.py)。待機表示は経過時間のみで制御
  D6  b2           … AI戦では人間=質問者(判定者)に固定。役割が正体を漏らさない
  D7  プロンプト版管理 … v0=素のGemini から。games.prompt_version に記録
  D8  ゲーム内固定  … プロンプト/遅延モデルはゲーム開始時に固定、記録
  D9  直接割当     … プロトタイプ用に force=ai / force=human で条件を直接指定できる
  §4  計測ログ     … char_count / compose_time_ms / displayed_delay_ms /
                      ground_truth_identity / backfilled / queue_wait_ms /
                      ai_switch_deadline_ms / assignment_mode / debriefed

構成:
  - シングル(HTTP): あなた(質問者) vs AI(回答者)。/start /ask /judge ...
  - オンライン(Socket.IO): クイックマッチ。人間同士 or AI補填。

起動方法:
  pip install flask flask-socketio google-genai
  python app.py
  ブラウザで http://127.0.0.1:5001 を開く
  (オンラインの動作確認は2つのタブ/端末で「グループ→公開」)
  プロトタイプの直接割当: http://127.0.0.1:5001/?force=ai または ?force=human
  (ポートを変えたい場合は環境変数 PORT を指定: PORT=5050 python app.py)
"""

import os
import random
import threading
import time
import uuid

from flask import Flask, render_template, request, jsonify, session
from flask_socketio import SocketIO, emit

import ai_backend
import database as db
import delay_model

app = Flask(__name__)
# デプロイ時は環境変数 SECRET_KEY を必ず設定する (Renderなら generateValue で自動生成)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")

# async_mode: ローカル開発は threading、Render等の本番は gevent
# (gunicorn の GeventWebSocketWorker と組み合わせる。render.yaml 参照)
socketio = SocketIO(
    app,
    async_mode=os.environ.get("SOCKETIO_ASYNC_MODE", "threading"),
    cors_allowed_origins="*")

# 1ゲームの質問回数(これを過ぎたら判定フェーズへ)
MAX_TURNS = 5

# 正体ドローのAI確率 (決定D2)。運用中に変更しないこと。
ALPHA_AI = 0.4

# 「再検索中…」表示を出す経過秒数。正体・切替と無関係に全員同一 (決定D4-4)
REQUEUE_NOTICE_S = 20.0

# プレイヤー識別子(1対1なので固定)。大人数化したら動的に振る。
HUMAN_KEY = "p_human"
OPPONENT_KEY = "p_opponent"
ASKER_KEY = "p_asker"
ANSWERER_KEY = "p_answerer"
QUICK_ENTRY = "quick"

# アプリ起動時にDBを用意
db.init_db()


@app.route("/")
def index():
    return render_template("index.html")


# ==========================================================================
#  シングル(HTTP) ― 計測・遅延・プロンプト版管理を追加
#
#  注: シングルは現状「必ずAI」(人間マッチング基盤はオンライン側のみ)。
#      その事実は assignment_mode='single_always_ai' として正直に記録する。
#      画面上は「ランダム」と案内しているが、正体が既知の実験者が使う
#      プロトタイプ用途では ?force=ai で明示するのが正しい使い方 (決定D9)。
# ==========================================================================

@app.route("/start", methods=["POST"])
def start():
    """新しいゲームを開始。プロンプト/遅延モデルの版をこの時点で固定する(D8)。"""
    data = request.json or {}
    forced = data.get("force") == "ai"  # プロトタイプの直接割当 (D9)
    session["game_id"] = "game_" + uuid.uuid4().hex[:12]
    session["opponent"] = "ai"
    session["assignment_mode"] = "forced" if forced else "single_always_ai"
    session["prompt_version"] = ai_backend.DEFAULT_PROMPT_VERSION
    session["delay_model_version"] = delay_model.DELAY_MODEL_VERSION
    session["history"] = []
    session["turn"] = 0
    session["saved"] = False  # 二重保存を防ぐフラグ
    return jsonify({"status": "started", "max_turns": MAX_TURNS})


@app.route("/ask", methods=["POST"])
def ask():
    """
    質問者(あなた)の質問を受け取り、回答者(AI)の答えと表示遅延を返す。
    クライアントは delay_ms が経過するまで回答を表示しない (b1の応答側)。
    """
    data = request.json or {}
    question = (data.get("question") or "").strip()
    if not question:
        return jsonify({"error": "質問が空です"}), 400
    compose_time_ms = data.get("compose_time_ms")

    history = session.get("history", [])
    turn = session.get("turn", 0)

    if turn >= MAX_TURNS:
        return jsonify({"error": "質問回数の上限です。判定してください。"}), 400

    history.append({
        "role": "questioner", "text": question,
        "compose_time_ms": compose_time_ms,
    })
    reply = ai_backend.generate_reply(
        history, prompt_version=session.get("prompt_version"))
    delay_s = delay_model.draw_reply_delay_s(len(reply))
    delay_ms = int(delay_s * 1000)
    history.append({
        "role": "ai", "text": reply,
        "displayed_delay_ms": delay_ms,
    })

    turn += 1
    session["history"] = history
    session["turn"] = turn

    return jsonify({
        "reply": reply,
        "delay_ms": delay_ms,
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
            ai_model=ai_backend.MODEL_NAME if answer == "ai" else None,
            result=result,
            ground_truth_identity=answer,
            backfilled=False,
            queue_wait_ms=None,           # シングルは待機なし
            ai_switch_deadline_ms=None,   # Wはクイックのみ
            prompt_version=session.get("prompt_version"),
            delay_model_version=session.get("delay_model_version"),
            assignment_mode=session.get("assignment_mode"),
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
                    game_id, turn, HUMAN_KEY, "question", h["text"],
                    compose_time_ms=h.get("compose_time_ms"))
            else:
                db.save_message(
                    game_id, turn, OPPONENT_KEY, "answer", h["text"],
                    reply_to=last_question_id,
                    displayed_delay_ms=h.get("displayed_delay_ms"))
        session["saved"] = True

    return jsonify({
        "correct": correct, "answer": answer,
        "your_guess": guess, "game_id": game_id,
    })


@app.route("/survey", methods=["POST"])
def survey():
    """任意アンケートを保存する。オンラインからは game_id を明示指定できる。"""
    data = request.json or {}
    game_id = data.get("game_id") or session.get("game_id")
    if not game_id:
        return jsonify({"error": "ゲームがありません"}), 400
    db.save_survey(
        game_id=game_id,
        respondent_id=data.get("respondent_id") or HUMAN_KEY,
        confidence=data.get("confidence"),
        basis=data.get("basis", ""),
        free_text=data.get("free_text", ""),
    )
    return jsonify({"status": "saved"})


@app.route("/messages", methods=["GET"])
def get_messages():
    """指定ゲームの会話ログを返す (注釈画面で「どの発言か」を選ぶ材料)。"""
    game_id = request.args.get("game_id") or session.get("game_id")
    if not game_id:
        return jsonify({"messages": []})
    with db._conn() as conn:
        rows = conn.execute(
            """SELECT id, turn, speaker_id, kind, content
               FROM messages WHERE game_id=? ORDER BY id""",
            (game_id,),
        ).fetchall()
    return jsonify({"messages": [dict(r) for r in rows]})


@app.route("/annotate", methods=["POST"])
def annotate():
    """特定の発言への注釈を保存する (決め手/違和感 など)。任意。"""
    data = request.json or {}
    game_id = data.get("game_id") or session.get("game_id")
    if not game_id:
        return jsonify({"error": "ゲームがありません"}), 400
    message_id = data.get("message_id")
    if message_id is None:
        return jsonify({"error": "message_idが必要です"}), 400
    db.save_annotation(
        game_id=game_id, message_id=message_id,
        annotator_id=data.get("annotator_id") or HUMAN_KEY,
        kind=data.get("kind", ""), free_text=data.get("free_text", ""),
    )
    return jsonify({"status": "saved"})


@app.route("/admin/export")
def admin_export():
    """
    全データをJSONで返す (研究データの回収用)。
    Render無料枠はファイルシステムが一時的で、再デプロイ・再起動・
    スピンダウンのたびにSQLiteが消えるため、セッション終了ごとに
    このエンドポイントを叩いてデータを手元に保存すること。

    使い方: 環境変数 EXPORT_TOKEN を設定した上で
      GET /admin/export?token=<EXPORT_TOKEN>
    EXPORT_TOKEN が未設定の場合は安全側に倒して常に403を返す。
    """
    expected = os.environ.get("EXPORT_TOKEN")
    if not expected or request.args.get("token", "") != expected:
        return jsonify({"error": "unauthorized"}), 403
    out = {}
    with db._conn() as conn:
        for table in ("games", "players", "messages",
                      "annotations", "surveys"):
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()
            out[table] = [dict(r) for r in rows]
    return jsonify(out)


# ==========================================================================
#  オンライン(クイックマッチ) ― W事前ドロー方式 + AI補填
#
#  流れ (決定D4):
#    1. join_quick で正体を引く: AI(確率α) / 人間(確率1-α)
#    2. 同時にAI切替デッドラインWを引く (全ゲーム共通の固定分布)
#    3. 人間ドロー: W到達まで人間(同じく人間ドローの待機者)とマッチを試みる
#         見つかる → 人対人ゲーム
#         W到達    → AI戦へ切替 (backfilled=1 を記録。沈黙補填の禁止)
#       AIドロー: 人間とはマッチせず、W到達でAI戦を開始
#         (開始タイミングの分布を正体間で近づけるため、AIドローも即開始しない)
#    4. 「再検索中…」表示は経過時間のみで制御し、正体と無関係に全員に出す
#
#  b2 (決定D6): AI戦では人間=質問者(判定者)に固定。
#    人対人では質問者/回答者をランダム割当 (判定するのは質問者のみなので、
#    回答者に正体情報が渡っても判定データは汚れない)。
#
#  プロトタイプ (決定D9): join_quick に force='ai' / force='human' を渡すと
#    ドローを使わず直接割当。force='ai' は即AI戦、force='human' は
#    人間が来るまで待ち続ける(AI切替なし)。assignment_mode='forced' で記録。
#
#  状態はサーバー側プロセス内メモリ。複数ワーカー化するなら Redis 等が必要。
# ==========================================================================

_waiting = []      # 待機エントリ {sid, identity, forced, joined_at, deadline_s, notified}
_rooms = {}        # room_id -> 部屋の状態 dict
_sid_room = {}     # sid -> room_id (逆引き)
_lock = threading.Lock()
_monitor_started = False


def _now_mono():
    return time.monotonic()


def _make_human_room(entry_a, entry_b):
    """人間ドロー2人から部屋を作る。役割はランダム割当。"""
    room_id = "room_" + uuid.uuid4().hex[:8]
    asker_e, answerer_e = random.sample([entry_a, entry_b], 2)
    now = _now_mono()
    _rooms[room_id] = {
        "game_id": "game_" + uuid.uuid4().hex[:12],
        "is_ai": False,
        "ground_truth": "human",
        "backfilled": False,
        "asker": asker_e["sid"],
        "answerer": answerer_e["sid"],
        "turn": 0,
        "history": [],
        "saved": False,
        # 計測 (質問者=判定者側の待ち時間とWを記録する)
        "queue_wait_ms": int((now - asker_e["joined_at"]) * 1000),
        "deadline_ms": (int(asker_e["deadline_s"] * 1000)
                        if asker_e["deadline_s"] is not None else None),
        "assignment_mode": "forced" if asker_e["forced"] else "random",
        "prompt_version": None,
        "delay_model_version": None,
    }
    _sid_room[asker_e["sid"]] = room_id
    _sid_room[answerer_e["sid"]] = room_id
    return room_id


def _make_ai_room(entry, backfilled):
    """AI戦の部屋を作る。b2: 人間は必ず質問者(判定者)。"""
    room_id = "room_" + uuid.uuid4().hex[:8]
    now = _now_mono()
    _rooms[room_id] = {
        "game_id": "game_" + uuid.uuid4().hex[:12],
        "is_ai": True,
        "ground_truth": "ai",
        "backfilled": backfilled,
        "asker": entry["sid"],
        "answerer": None,  # AI
        "turn": 0,
        "history": [],
        "saved": False,
        "queue_wait_ms": int((now - entry["joined_at"]) * 1000),
        "deadline_ms": (int(entry["deadline_s"] * 1000)
                        if entry["deadline_s"] is not None else None),
        "assignment_mode": "forced" if entry["forced"] else "random",
        # D8: プロンプト/遅延モデルはゲーム開始時に固定して記録
        "prompt_version": ai_backend.DEFAULT_PROMPT_VERSION,
        "delay_model_version": delay_model.DELAY_MODEL_VERSION,
    }
    _sid_room[entry["sid"]] = room_id
    return room_id


def _notify_match(room_id):
    """部屋の参加者に役割を通知する。ペイロードは正体情報を含まない。"""
    room = _rooms[room_id]
    socketio.emit("match_found",
                  {"role": "asker", "max_turns": MAX_TURNS},
                  to=room["asker"])
    if not room["is_ai"]:
        socketio.emit("match_found",
                      {"role": "answerer", "max_turns": MAX_TURNS},
                      to=room["answerer"])


def _queue_monitor():
    """
    待機列の監視ループ (バックグラウンド)。
      - 経過 REQUEUE_NOTICE_S 秒で「再検索中…」を通知 (正体と無関係・全員同一)
      - W(deadline) 到達でAI戦へ切替
    """
    while True:
        socketio.sleep(0.5)
        to_ai = []
        with _lock:
            now = _now_mono()
            for entry in list(_waiting):
                elapsed = now - entry["joined_at"]
                if (not entry["notified"]) and elapsed >= REQUEUE_NOTICE_S:
                    entry["notified"] = True
                    socketio.emit(
                        "waiting_status",
                        {"msg": "見つかりません。再検索しています…"},
                        to=entry["sid"])
                if (entry["deadline_s"] is not None
                        and elapsed >= entry["deadline_s"]):
                    _waiting.remove(entry)
                    to_ai.append(entry)
        for entry in to_ai:
            backfilled = (entry["identity"] == "human")
            with _lock:
                room_id = _make_ai_room(entry, backfilled=backfilled)
            _notify_match(room_id)


def _ensure_monitor():
    global _monitor_started
    with _lock:
        if not _monitor_started:
            _monitor_started = True
            socketio.start_background_task(_queue_monitor)


@socketio.on("connect")
def _on_connect(auth=None):
    # 接続しただけでは何もしない。マッチングは join_quick で明示的に行う。
    pass


@socketio.on("join_quick")
def _on_join_quick(data=None):
    """
    クイックマッチ待機列に入る。
    data.force: 'ai' | 'human' (プロトタイプの直接割当 D9。省略時は本番のαドロー)
    """
    _ensure_monitor()
    sid = request.sid
    force = (data or {}).get("force")

    if force == "ai":
        # プロトタイプ: 即AI戦 (待機なし)
        entry = {"sid": sid, "identity": "ai", "forced": True,
                 "joined_at": _now_mono(), "deadline_s": None,
                 "notified": False}
        with _lock:
            room_id = _make_ai_room(entry, backfilled=False)
        _notify_match(room_id)
        return

    if force == "human":
        identity, deadline, forced = "human", None, True  # AI切替なしで待つ
    else:
        # 本番: 正体ドロー (D2) + W事前ドロー (D4)
        identity = "ai" if random.random() < ALPHA_AI else "human"
        deadline = delay_model.draw_ai_switch_deadline_s()
        forced = False

    entry = {"sid": sid, "identity": identity, "forced": forced,
             "joined_at": _now_mono(), "deadline_s": deadline,
             "notified": False}

    partner = None
    with _lock:
        # 二重登録の除去(再送対策)
        _waiting[:] = [e for e in _waiting if e["sid"] != sid]
        if identity == "human":
            # 人間ドロー同士のみマッチ可能 (AIドローの待機者とは組まない)
            for e in _waiting:
                if e["identity"] == "human":
                    partner = e
                    break
            if partner is not None:
                _waiting.remove(partner)
            else:
                _waiting.append(entry)
        else:
            # AIドロー: 人間とは組まず、W到達を待つ
            _waiting.append(entry)

    if partner is None:
        emit("waiting", {"msg": "対戦相手を探しています…"})
        return

    with _lock:
        room_id = _make_human_room(partner, entry)
    _notify_match(room_id)


def _ai_reply_task(room_id):
    """AI戦の回答生成タスク。遅延を挿入してから回答を送る (b1)。"""
    room = _rooms.get(room_id)
    if not room:
        return
    reply = ai_backend.generate_reply(
        [{"role": ("questioner" if h["role"] == "questioner" else "ai"),
          "text": h["text"]} for h in room["history"]],
        prompt_version=room["prompt_version"])
    delay_s = delay_model.draw_reply_delay_s(len(reply))
    socketio.sleep(delay_s)

    room = _rooms.get(room_id)  # 待っている間に部屋が消えていないか再確認
    if not room:
        return
    room["history"].append({
        "role": "answerer", "text": reply,
        "displayed_delay_ms": int(delay_s * 1000),
    })
    room["turn"] += 1
    can_judge = room["turn"] >= MAX_TURNS
    socketio.emit("answer",
                  {"text": reply, "turn": room["turn"],
                   "max_turns": MAX_TURNS, "can_judge": can_judge},
                  to=room["asker"])


@socketio.on("submit_question")
def _on_question(data):
    """質問者の質問を受け取る。人間戦なら回答者へ転送、AI戦なら生成タスクを起動。"""
    sid = request.sid
    room = _rooms.get(_sid_room.get(sid))
    if not room or sid != room["asker"]:
        return
    if room["turn"] >= MAX_TURNS:
        return
    text = ((data or {}).get("text") or "").strip()
    if not text:
        return
    room["history"].append({
        "role": "questioner", "text": text,
        "compose_time_ms": (data or {}).get("compose_time_ms"),
    })
    if room["is_ai"]:
        socketio.start_background_task(
            _ai_reply_task, _sid_room.get(sid))
    else:
        socketio.emit("question",
                      {"text": text, "turn": room["turn"] + 1,
                       "max_turns": MAX_TURNS},
                      to=room["answerer"])


@socketio.on("submit_answer")
def _on_answer(data):
    """(人間戦のみ) 回答者の回答を質問者へ転送する。ターンを1進める。"""
    sid = request.sid
    room = _rooms.get(_sid_room.get(sid))
    if not room or room["is_ai"] or sid != room["answerer"]:
        return
    text = ((data or {}).get("text") or "").strip()
    if not text:
        return
    room["history"].append({
        "role": "answerer", "text": text,
        "compose_time_ms": (data or {}).get("compose_time_ms"),
    })
    room["turn"] += 1
    can_judge = room["turn"] >= MAX_TURNS
    socketio.emit("answer",
                  {"text": text, "turn": room["turn"],
                   "max_turns": MAX_TURNS, "can_judge": can_judge},
                  to=room["asker"])
    socketio.emit("turn_update",
                  {"turn": room["turn"], "max_turns": MAX_TURNS},
                  to=room["answerer"])


def _save_online_game(room, guess, correct):
    """オンラインゲームをDBに保存する (人間戦・AI戦の両対応)。"""
    if room.get("saved"):
        return
    game_id = room["game_id"]
    is_ai = room["is_ai"]
    result = "human_correct" if correct else "human_wrong"
    db.save_game(
        game_id=game_id, entry_type=QUICK_ENTRY, player_count=2,
        ai_present=is_ai, ai_count=1 if is_ai else 0,
        ai_model=ai_backend.MODEL_NAME if is_ai else None,
        result=result,
        ground_truth_identity=room["ground_truth"],
        backfilled=room["backfilled"],
        queue_wait_ms=room["queue_wait_ms"],
        ai_switch_deadline_ms=room["deadline_ms"],
        prompt_version=room["prompt_version"],
        delay_model_version=room["delay_model_version"],
        assignment_mode=room["assignment_mode"],
    )
    db.save_player(game_id, ASKER_KEY, role="human",
                   vote_target=ANSWERER_KEY, correct=correct)
    db.save_player(game_id, ANSWERER_KEY,
                   role="ai" if is_ai else "human",
                   vote_target=None, correct=None)
    turn = 0
    last_qid = None
    for h in room["history"]:
        if h["role"] == "questioner":
            turn += 1
            last_qid = db.save_message(
                game_id, turn, ASKER_KEY, "question", h["text"],
                compose_time_ms=h.get("compose_time_ms"))
        else:
            db.save_message(
                game_id, turn, ANSWERER_KEY, "answer", h["text"],
                reply_to=last_qid,
                compose_time_ms=h.get("compose_time_ms"),
                displayed_delay_ms=h.get("displayed_delay_ms"))
    room["saved"] = True


@socketio.on("submit_judge")
def _on_judge(data):
    """質問者の判定を受け取り、保存し、参加者に結果を返す。"""
    sid = request.sid
    room = _rooms.get(_sid_room.get(sid))
    if not room or sid != room["asker"]:
        return
    guess = (data or {}).get("guess", "")
    answer = room["ground_truth"]
    correct = (guess == answer)
    _save_online_game(room, guess, correct)

    payload = {"correct": correct, "answer": answer,
               "your_guess": guess, "game_id": room["game_id"]}
    socketio.emit("result", {**payload, "you_are": "asker"}, to=room["asker"])
    if not room["is_ai"]:
        socketio.emit("result", {**payload, "you_are": "answerer"},
                      to=room["answerer"])


@socketio.on("disconnect")
def _on_disconnect(*args):
    """切断時のクリーンアップ。待機列/部屋から除去し、相手に通知する。"""
    sid = request.sid
    with _lock:
        _waiting[:] = [e for e in _waiting if e["sid"] != sid]
    room_id = _sid_room.pop(sid, None)
    if room_id and room_id in _rooms:
        room = _rooms[room_id]
        if not room["is_ai"]:
            other = (room["answerer"] if sid == room["asker"]
                     else room["asker"])
            _sid_room.pop(other, None)
            socketio.emit("opponent_left", {}, to=other)
        _rooms.pop(room_id, None)


if __name__ == "__main__":
    # ローカル開発用の起動 (本番は gunicorn 経由: render.yaml 参照)
    # デフォルトを5001に変更: macOSの「AirPlay受信」がポート5000を
    # 占有していることが多く、5000のままだと起動に失敗しやすいため。
    port = int(os.environ.get("PORT", 5001))
    socketio.run(app, host="0.0.0.0", port=port,
                 debug=os.environ.get("FLASK_DEBUG", "1") == "1")
