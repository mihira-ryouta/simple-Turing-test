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
  ブラウザで http://127.0.0.1:5000 を開く
  (オンラインの動作確認は2つのタブ/端末で「グループ→公開」)
  プロトタイプの直接割当: http://127.0.0.1:5000/?force=ai または ?force=human
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


@app.route("/about")
def about():
    """プロジェクト紹介ページ (チューリングテストの説明・研究趣旨・データの取り扱い)。"""
    return render_template("about.html")


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
_pending_private = {}  # 合言葉 -> {sid, mode, created_at} (ゲスト待ちのプライベート部屋)
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
        "entry_type": QUICK_ENTRY,
        "is_ai": False,
        "ground_truth": "human",
        "backfilled": False,
        "asker": asker_e["sid"],
        "answerer": answerer_e["sid"],
        "spectator": None,
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
        "entry_type": QUICK_ENTRY,
        "is_ai": True,
        "ground_truth": "ai",
        "backfilled": backfilled,
        "asker": entry["sid"],
        "answerer": None,  # AI
        "spectator": None,
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
    if room["answerer"] is not None:
        socketio.emit("match_found",
                      {"role": "answerer", "max_turns": MAX_TURNS},
                      to=room["answerer"])
    if room.get("spectator") is not None:
        # プライベート・シャッフルでAIに置き換えられたホスト。観戦モード。
        socketio.emit("match_found",
                      {"role": "spectator", "max_turns": MAX_TURNS},
                      to=room["spectator"])


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


# --------------------------------------------------------------------------
#  プライベートマッチ (合言葉制・1対1)
#
#  2つのモード (部屋を作るホストが選ぶ):
#    - human   … 人間同士の対戦。役割(質問者/回答者)はランダム割当
#    - shuffle … ゲスト=質問者(判定者)固定。回答者は「ホスト」か「AI」の
#                どちらかを確率0.5で決める。AIになった場合、ホストは観戦モード
#                (会話は見えるが発言できない)。ゲストには結果までどちらか
#                分からない —— 「友達かAIか」を当てるモード。
#
#  研究データ上の注意:
#    - entry_type='private' で記録し、主解析(クイック)と区別する
#    - humanモードは両者が知り合いで正体既知のため判定データとしては参考値
#    - shuffleモードのゲスト判定は「候補が {友達, AI} と知っている」条件付き
#      データとして扱う (assignment_mode='private_shuffle')
#    - 3人以上のグループ戦は次フェーズ (この実装は1対1の土台)
# --------------------------------------------------------------------------

PRIVATE_ENTRY = "private"
PRIVATE_SHUFFLE_AI_P = 0.5  # shuffleモードで回答者がAIになる確率


def _make_private_room(host_sid, guest_sid, mode):
    """プライベート部屋を作る。mode: 'human' | 'shuffle'"""
    room_id = "room_" + uuid.uuid4().hex[:8]
    base = {
        "game_id": "game_" + uuid.uuid4().hex[:12],
        "entry_type": PRIVATE_ENTRY,
        "backfilled": False,
        "spectator": None,
        "turn": 0,
        "history": [],
        "saved": False,
        "queue_wait_ms": None,       # プライベートは待機時間の意味が異なるため記録しない
        "deadline_ms": None,         # Wなし
        "prompt_version": None,
        "delay_model_version": None,
    }
    if mode == "human":
        asker, answerer = random.sample([host_sid, guest_sid], 2)
        room = {**base,
                "is_ai": False, "ground_truth": "human",
                "asker": asker, "answerer": answerer,
                "assignment_mode": "private_human"}
    else:  # shuffle: ゲストが判定者。回答者はホストかAIか。
        is_ai = random.random() < PRIVATE_SHUFFLE_AI_P
        room = {**base,
                "is_ai": is_ai,
                "ground_truth": "ai" if is_ai else "human",
                "asker": guest_sid,
                "answerer": None if is_ai else host_sid,
                "spectator": host_sid if is_ai else None,
                "assignment_mode": "private_shuffle"}
        if is_ai:
            # D8: AI戦なのでプロンプト/遅延モデルを固定して記録
            room["prompt_version"] = ai_backend.DEFAULT_PROMPT_VERSION
            room["delay_model_version"] = delay_model.DELAY_MODEL_VERSION
    _rooms[room_id] = room
    for sid in (host_sid, guest_sid):
        _sid_room[sid] = room_id
    return room_id


@socketio.on("create_private")
def _on_create_private(data):
    """ホストが合言葉で部屋を作る。ゲストが来るまで待機。"""
    sid = request.sid
    key = ((data or {}).get("key") or "").strip()
    mode = (data or {}).get("mode", "human")
    if not key:
        emit("private_error", {"msg": "合言葉を入力してください"})
        return
    if mode not in ("human", "shuffle"):
        emit("private_error", {"msg": "不正なモードです"})
        return
    with _lock:
        if key in _pending_private:
            emit("private_error",
                 {"msg": "その合言葉は使用中です。別の合言葉にしてください"})
            return
        _pending_private[key] = {"sid": sid, "mode": mode,
                                 "created_at": _now_mono()}
    emit("private_created",
         {"key": key, "msg": "合言葉を友達に伝えて、参加を待っています…"})


@socketio.on("join_private")
def _on_join_private(data):
    """ゲストが合言葉で部屋に入る。ホストが待っていればゲーム開始。"""
    sid = request.sid
    key = ((data or {}).get("key") or "").strip()
    if not key:
        emit("private_error", {"msg": "合言葉を入力してください"})
        return
    with _lock:
        # グループの合言葉が優先 (同名は作成時に弾いているので競合しない)
        g = _pending_groups.get(key)
        if g is not None and not g["started"]:
            if sid in g["members"]:
                emit("private_error", {"msg": "すでに参加しています"})
                return
            g["members"].append(sid)
            full = len(g["members"]) >= g["config"]["humans"]
            _broadcast_lobby(key)
            if full:
                _start_group(key)
            return
        pending = _pending_private.pop(key, None)
        if pending is None:
            emit("private_error",
                 {"msg": "その合言葉の部屋が見つかりません。"
                         "ホストが先に部屋を作っているか確認してください"})
            return
        if pending["sid"] == sid:
            # ホストが自分の部屋に参加しようとした場合は戻す
            _pending_private[key] = pending
            emit("private_error", {"msg": "自分が作った部屋には参加できません"})
            return
        room_id = _make_private_room(pending["sid"], sid, pending["mode"])
    _notify_match(room_id)


@socketio.on("cancel_private")
def _on_cancel_private(data=None):
    """ホストが待機をやめる。"""
    sid = request.sid
    with _lock:
        for key, pending in list(_pending_private.items()):
            if pending["sid"] == sid:
                _pending_private.pop(key, None)
        for key, g in list(_pending_groups.items()):
            if g["host_sid"] == sid:
                for m in g["members"]:
                    if m != sid:
                        socketio.emit("private_error",
                                      {"msg": "ホストが部屋を閉じました"}, to=m)
                _pending_groups.pop(key, None)
            elif sid in g["members"]:
                g["members"].remove(sid)
                _broadcast_lobby(key)


def _emit_spectate(room, kind, text):
    """観戦者(shuffleでAIに置き換えられたホスト)に会話を中継する。"""
    if room.get("spectator") is not None:
        socketio.emit("spect_message", {"kind": kind, "text": text},
                      to=room["spectator"])



# --------------------------------------------------------------------------
#  グループ戦 (プライベート・合言葉制・インタビューパネル形式)
#
#  構造 (非対称型の大人数拡張。1対1を内包する):
#    - 判定者1人 (参加した人間からランダム選出)
#    - 回答者パネル = 残りの人間 + AI(ホスト設定数) + AIのふり役(人間から抽選)
#    - 判定者が質問 → パネル全員が匿名(プレイヤーA/B/C...)で回答 × MAX_TURNS
#    - 最後に判定者が全員を人間/AIにラベル付け。正答数がスコア
#    - 回答者の勝利条件: 人間=人間と見抜かれる / AIのふり役=AIと誤認させる
#
#  配役モード (roleplay=true):
#    全回答者(AI含む)に役柄カードを配布し、なりきって回答する遊びモード。
#    ※設計段階で「研究価値が薄い」として主線から除外された案のため、
#      entry_type='private_group' + games.prompt_version='rp-v1' で
#      研究データから分離できる形で実装している。
#
#  研究データ上の注意:
#    - クイックのグループ(ランダム構成マッチング)は次フェーズ。
#      N人同時キューの成立性とグループでのタイミング中立化は別設計問題。
#    - ホストが判定者に選ばれた場合、ホストは構成(AI数等)を知っているため
#      判定の難易度が下がる。games.result とは別に解析時に考慮すること。
# --------------------------------------------------------------------------

GROUP_ENTRY = "private_group"
GROUP_MAX_HUMANS = 6
GROUP_MAX_AI = 3

PERSONA_CARDS = [
    "関西弁のおばちゃん", "中二病の高校生", "丁寧すぎる執事",
    "語尾が「にゃ」になる猫", "昭和の頑固親父", "ポエムを語りがちな詩人",
    "テンション高めのギャル", "言葉少なな忍者",
]

_pending_groups = {}  # 合言葉 -> {host_sid, config, members(sid一覧), started}


def _alias_name(i):
    return "プレイヤー" + chr(ord("A") + i)


def _group_room(room_id):
    room = _rooms.get(room_id)
    return room if room and room.get("group") else None


@socketio.on("create_group")
def _on_create_group(data):
    """ホストがグループ部屋を作る。config: key/humans/ai_count/human_as_ai/roleplay"""
    sid = request.sid
    d = data or {}
    key = (d.get("key") or "").strip()
    try:
        humans = int(d.get("humans", 2))
        ai_count = int(d.get("ai_count", 1))
        haa = int(d.get("human_as_ai", 0))
    except (TypeError, ValueError):
        emit("private_error", {"msg": "人数の指定が不正です"})
        return
    roleplay = bool(d.get("roleplay", False))
    if not key:
        emit("private_error", {"msg": "合言葉を入力してください"})
        return
    if not (2 <= humans <= GROUP_MAX_HUMANS):
        emit("private_error", {"msg": f"人間は2〜{GROUP_MAX_HUMANS}人です"})
        return
    if not (0 <= ai_count <= GROUP_MAX_AI):
        emit("private_error", {"msg": f"AIは0〜{GROUP_MAX_AI}体です"})
        return
    # 回答者パネル = (humans - 判定者1) + ai_count は最低1必要
    if (humans - 1) + ai_count < 1:
        emit("private_error", {"msg": "回答者が0人になる設定です"})
        return
    if not (0 <= haa <= humans - 1):
        emit("private_error", {"msg": "AIのふり役が人間の回答者数を超えています"})
        return
    with _lock:
        if key in _pending_private or key in _pending_groups:
            emit("private_error", {"msg": "その合言葉は使用中です"})
            return
        _pending_groups[key] = {
            "host_sid": sid, "members": [sid], "started": False,
            "config": {"humans": humans, "ai_count": ai_count,
                       "human_as_ai": haa, "roleplay": roleplay},
        }
    emit("group_lobby", {"key": key, "joined": 1, "needed": humans,
                         "is_host": True})


def _broadcast_lobby(key):
    g = _pending_groups.get(key)
    if not g:
        return
    for i, m in enumerate(g["members"]):
        socketio.emit("group_lobby",
                      {"key": key, "joined": len(g["members"]),
                       "needed": g["config"]["humans"],
                       "is_host": (m == g["host_sid"])},
                      to=m)


def _start_group(key):
    """人数が揃ったら(またはホスト開始で)ゲームを構成して開始する。"""
    g = _pending_groups.pop(key, None)
    if not g or g["started"]:
        return
    g["started"] = True
    cfg = g["config"]
    members = list(g["members"])

    judge = random.choice(members)
    human_answerers = [m for m in members if m != judge]
    # AIのふり役を人間回答者から抽選 (人数が足りなければ全員まで)
    haa_n = min(cfg["human_as_ai"], len(human_answerers))
    haa_set = set(random.sample(human_answerers, haa_n)) if haa_n else set()

    panel = []
    for m in human_answerers:
        panel.append({"sid": m,
                      "kind": "human_as_ai" if m in haa_set else "human",
                      "persona": None, "left": False})
    for _ in range(cfg["ai_count"]):
        panel.append({"sid": None, "kind": "ai", "persona": None,
                      "left": False})
    random.shuffle(panel)
    personas = random.sample(PERSONA_CARDS, len(panel)) \
        if cfg["roleplay"] else [None] * len(panel)
    for i, p in enumerate(panel):
        p["alias"] = _alias_name(i)
        p["persona"] = personas[i]

    room_id = "room_" + uuid.uuid4().hex[:8]
    has_ai = cfg["ai_count"] > 0
    _rooms[room_id] = {
        "game_id": "game_" + uuid.uuid4().hex[:12],
        "group": True,
        "entry_type": GROUP_ENTRY,
        "roleplay": cfg["roleplay"],
        "judge": judge,
        "host": g["host_sid"],
        "panel": panel,
        "turn": 0,
        "pending": set(),        # 今ターンで未回答のalias
        "current_q": None,       # {"text","compose_time_ms"}
        "log": [],               # 保存用の時系列ログ
        "saved": False,
        "prompt_version": (ai_backend.ROLEPLAY_PROMPT_VERSION
                           if cfg["roleplay"] else
                           ai_backend.DEFAULT_PROMPT_VERSION) if has_ai else None,
        "delay_model_version": (delay_model.DELAY_MODEL_VERSION
                                if has_ai else None),
    }
    _sid_room[judge] = room_id
    for p in panel:
        if p["sid"] is not None:
            _sid_room[p["sid"]] = room_id

    # 通知: 判定者にはパネルの匿名一覧、回答者には自分の役どころだけ
    socketio.emit("group_started",
                  {"role": "judge", "max_turns": MAX_TURNS,
                   "aliases": [p["alias"] for p in panel]},
                  to=judge)
    for p in panel:
        if p["sid"] is None:
            continue
        socketio.emit("group_started",
                      {"role": "answerer", "max_turns": MAX_TURNS,
                       "alias": p["alias"],
                       "act_as_ai": p["kind"] == "human_as_ai",
                       "persona": p["persona"]},
                      to=p["sid"])


@socketio.on("start_group")
def _on_start_group(data):
    """ホストが手動開始する (2人以上集まっていれば人数未満でも開始可)。"""
    sid = request.sid
    key = ((data or {}).get("key") or "").strip()
    with _lock:
        g = _pending_groups.get(key)
        if not g or g["host_sid"] != sid:
            return
        if len(g["members"]) < 2:
            socketio.emit("private_error",
                          {"msg": "開始には2人以上必要です"}, to=sid)
            return
        # 実際に集まった人数で構成し直す
        g["config"]["humans"] = len(g["members"])
        g["config"]["human_as_ai"] = min(g["config"]["human_as_ai"],
                                         len(g["members"]) - 1)
    _start_group(key)


def _group_ai_task(room_id, panel_index):
    """グループ戦のAIパネリスト1体ぶんの回答生成 (b1遅延つき)。"""
    room = _group_room(room_id)
    if not room:
        return
    p = room["panel"][panel_index]
    history = [{"role": "questioner", "text": e["text"]}
               for e in room["log"] if e["kind"] == "question"]
    reply = ai_backend.generate_reply(
        history, prompt_version=room["prompt_version"],
        persona=p["persona"])
    delay_s = delay_model.draw_reply_delay_s(len(reply))
    socketio.sleep(delay_s)
    room = _group_room(room_id)
    if not room:
        return
    _group_receive_answer(room_id, p["alias"], reply,
                          compose_time_ms=None,
                          displayed_delay_ms=int(delay_s * 1000))


def _group_receive_answer(room_id, alias, text,
                          compose_time_ms, displayed_delay_ms):
    """人間/AI共通の回答受付。判定者へ中継し、全員揃ったらターンを締める。"""
    room = _group_room(room_id)
    if not room or alias not in room["pending"]:
        return
    room["pending"].discard(alias)
    room["log"].append({"kind": "answer", "turn": room["turn"],
                        "alias": alias, "text": text,
                        "compose_time_ms": compose_time_ms,
                        "displayed_delay_ms": displayed_delay_ms})
    active = [p for p in room["panel"]
              if not p["left"] or p["sid"] is None]
    socketio.emit("group_answer",
                  {"alias": alias, "text": text, "turn": room["turn"],
                   "answered": len(active) - len(room["pending"]),
                   "panel_size": len(active)},
                  to=room["judge"])
    if not room["pending"]:
        can_judge = room["turn"] >= MAX_TURNS
        socketio.emit("group_turn_complete",
                      {"turn": room["turn"], "max_turns": MAX_TURNS,
                       "can_judge": can_judge},
                      to=room["judge"])


@socketio.on("submit_group_question")
def _on_group_question(data):
    """判定者の質問。人間回答者へ配信し、AI回答タスクを起動する。"""
    sid = request.sid
    room_id = _sid_room.get(sid)
    room = _group_room(room_id)
    if not room or sid != room["judge"]:
        return
    if room["pending"] or room["turn"] >= MAX_TURNS:
        return  # 前ターン未完 or 上限
    text = ((data or {}).get("text") or "").strip()
    if not text:
        return
    room["turn"] += 1
    room["log"].append({"kind": "question", "turn": room["turn"],
                        "alias": None, "text": text,
                        "compose_time_ms": (data or {}).get("compose_time_ms"),
                        "displayed_delay_ms": None})
    room["pending"] = {p["alias"] for p in room["panel"] if not p["left"]}
    for i, p in enumerate(room["panel"]):
        if p["left"]:
            continue
        if p["sid"] is None:
            socketio.start_background_task(_group_ai_task, room_id, i)
        else:
            socketio.emit("group_question",
                          {"text": text, "turn": room["turn"],
                           "max_turns": MAX_TURNS},
                          to=p["sid"])


@socketio.on("submit_group_answer")
def _on_group_answer(data):
    """(人間の回答者) 自分のaliasで回答する。"""
    sid = request.sid
    room_id = _sid_room.get(sid)
    room = _group_room(room_id)
    if not room:
        return
    me = next((p for p in room["panel"] if p["sid"] == sid), None)
    if me is None:
        return
    text = ((data or {}).get("text") or "").strip()
    if not text:
        return
    _group_receive_answer(room_id, me["alias"], text,
                          compose_time_ms=(data or {}).get("compose_time_ms"),
                          displayed_delay_ms=None)


def _save_group_game(room, labels, n_correct, n_total):
    if room.get("saved"):
        return
    game_id = room["game_id"]
    ai_n = sum(1 for p in room["panel"] if p["kind"] == "ai")
    db.save_game(
        game_id=game_id, entry_type=GROUP_ENTRY,
        player_count=1 + len(room["panel"]),
        ai_present=ai_n > 0, ai_count=ai_n,
        ai_model=ai_backend.MODEL_NAME if ai_n else None,
        result=f"{n_correct}/{n_total}",
        ground_truth_identity=None,   # グループは1対1のground_truthを持たない
        backfilled=False,
        queue_wait_ms=None, ai_switch_deadline_ms=None,
        prompt_version=room["prompt_version"],
        delay_model_version=room["delay_model_version"],
        assignment_mode="private_group",
    )
    db.save_player(game_id, "p_judge", role="human",
                   vote_target=None, correct=None, alias="判定者")
    for p in room["panel"]:
        label = labels.get(p["alias"])
        truth_is_ai = (p["kind"] == "ai")
        correct = (None if label is None
                   else (label == "ai") == truth_is_ai)
        db.save_player(game_id, "p_" + p["alias"], role=p["kind"],
                       vote_target=None, correct=correct,
                       judged_as=label, persona=p["persona"],
                       alias=p["alias"])
    # メッセージ: 質問→そのターンの各回答(reply_to=質問id)
    qid_by_turn = {}
    for e in room["log"]:
        if e["kind"] == "question":
            qid_by_turn[e["turn"]] = db.save_message(
                game_id, e["turn"], "p_judge", "question", e["text"],
                compose_time_ms=e["compose_time_ms"])
        else:
            db.save_message(
                game_id, e["turn"], "p_" + e["alias"], "answer",
                e["text"], reply_to=qid_by_turn.get(e["turn"]),
                compose_time_ms=e["compose_time_ms"],
                displayed_delay_ms=e["displayed_delay_ms"])
    room["saved"] = True


@socketio.on("submit_group_judgement")
def _on_group_judgement(data):
    """判定者の最終ラベル付け {labels: {alias: 'ai'|'human'}} を受け取る。"""
    sid = request.sid
    room_id = _sid_room.get(sid)
    room = _group_room(room_id)
    if not room or sid != room["judge"]:
        return
    if room["turn"] < 1:
        return
    labels = (data or {}).get("labels") or {}
    reveal = []
    n_correct = 0
    n_total = 0
    for p in room["panel"]:
        label = labels.get(p["alias"])
        truth_is_ai = (p["kind"] == "ai")
        ok = (label == "ai") == truth_is_ai if label else None
        if ok is not None:
            n_total += 1
            n_correct += int(ok)
        reveal.append({"alias": p["alias"],
                       "truth": "ai" if truth_is_ai else "human",
                       "was_acting": p["kind"] == "human_as_ai",
                       "judged_as": label, "correct": ok,
                       "persona": p["persona"]})
    _save_group_game(room, labels, n_correct, n_total)

    socketio.emit("group_result",
                  {"you_are": "judge", "score": n_correct,
                   "total": n_total, "reveal": reveal,
                   "game_id": room["game_id"]},
                  to=room["judge"])
    for p in room["panel"]:
        if p["sid"] is None:
            continue
        label = labels.get(p["alias"])
        if p["kind"] == "human_as_ai":
            won = (label == "ai")     # AIと誤認させたら勝ち
        else:
            won = (label == "human")  # 人間と見抜かれたら勝ち
        socketio.emit("group_result",
                      {"you_are": "answerer", "alias": p["alias"],
                       "your_kind": p["kind"], "judged_as": label,
                       "you_won": won, "reveal": reveal,
                       "game_id": room["game_id"]},
                      to=p["sid"])
    # 部屋の後始末
    _sid_room.pop(room["judge"], None)
    for p in room["panel"]:
        if p["sid"] is not None:
            _sid_room.pop(p["sid"], None)
    _rooms.pop(room_id, None)


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
    _emit_spectate(room, "answer", reply)


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
    _emit_spectate(room, "question", text)
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
        game_id=game_id, entry_type=room.get("entry_type", QUICK_ENTRY),
        player_count=2,
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
    if room["answerer"] is not None:
        socketio.emit("result", {**payload, "you_are": "answerer"},
                      to=room["answerer"])
    if room.get("spectator") is not None:
        socketio.emit("result", {**payload, "you_are": "spectator"},
                      to=room["spectator"])


@socketio.on("disconnect")
def _on_disconnect(*args):
    """切断時のクリーンアップ。待機列/保留部屋/部屋から除去し、相手に通知する。"""
    sid = request.sid
    with _lock:
        _waiting[:] = [e for e in _waiting if e["sid"] != sid]
        for key, pending in list(_pending_private.items()):
            if pending["sid"] == sid:
                _pending_private.pop(key, None)
        for key, g in list(_pending_groups.items()):
            if g["host_sid"] == sid:
                for m in g["members"]:
                    if m != sid:
                        socketio.emit("private_error",
                                      {"msg": "ホストが退出しました"}, to=m)
                _pending_groups.pop(key, None)
            elif sid in g["members"]:
                g["members"].remove(sid)
                _broadcast_lobby(key)
    room_id = _sid_room.pop(sid, None)
    if room_id and room_id in _rooms:
        room = _rooms[room_id]
        if room.get("group"):
            if sid == room["judge"]:
                # 判定者が抜けたらゲーム終了
                for p in room["panel"]:
                    if p["sid"] is not None:
                        _sid_room.pop(p["sid"], None)
                        socketio.emit("opponent_left", {}, to=p["sid"])
                _rooms.pop(room_id, None)
            else:
                # 回答者が抜けたらパネルから外して続行
                for p in room["panel"]:
                    if p["sid"] == sid:
                        p["left"] = True
                        room["pending"].discard(p["alias"])
                if room["pending"] == set() and room["turn"] >= 1:
                    socketio.emit("group_turn_complete",
                                  {"turn": room["turn"],
                                   "max_turns": MAX_TURNS,
                                   "can_judge": room["turn"] >= MAX_TURNS},
                                  to=room["judge"])
            return
        if sid == room.get("spectator"):
            # 観戦者(shuffleのホスト)が抜けてもゲームは続行できる
            room["spectator"] = None
            return
        others = [s for s in (room["asker"], room["answerer"],
                              room.get("spectator"))
                  if s is not None and s != sid]
        for other in others:
            _sid_room.pop(other, None)
            socketio.emit("opponent_left", {}, to=other)
        _rooms.pop(room_id, None)


if __name__ == "__main__":
    # ローカル開発用の起動 (本番は gunicorn 経由: render.yaml 参照)
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port,
                 debug=os.environ.get("FLASK_DEBUG", "1") == "1")
