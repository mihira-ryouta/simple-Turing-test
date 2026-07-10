# -*- coding: utf-8 -*-
"""
test_online.py ― アップグレード版の自動テスト。

検証すること:
  1. スキーマ: 計測列が games/messages に存在する
  2. 人対人: force=human 2人がマッチし、質問→回答→判定→保存
     (compose_time_ms / char_count / ground_truth='human' が記録される)
  3. 強制AI戦: force=ai で即マッチ、ダミーAIが返答し、判定→保存
     (displayed_delay_ms / assignment_mode='forced' が記録される)
  4. W切替: αドローで人間を引いた待機者が、W到達でAI戦へ切替
     (backfilled=1 が記録される)

注意:
  - SocketIOTestClient はサーバーロジックのみ保証する。ネットワーク伝送と
    ブラウザJSは保証しないので、実機確認(2タブ)は別途必須。
  - GAME_FAST_DELAY=1 で遅延をほぼゼロにしてテストする。
    研究データ収集時にこの環境変数を設定してはならない。

実行: python test_online.py
"""

import os
import time

# アプリを import する前に環境を整える
os.environ["GAME_FAST_DELAY"] = "1"          # 遅延ほぼゼロ (テスト専用)
os.environ["GAME_DB_PATH"] = "test_game.db"  # 本物のDBを汚さない
if os.path.exists("test_game.db"):
    os.remove("test_game.db")

import app as app_module  # noqa: E402
import database as db     # noqa: E402

app = app_module.app
socketio = app_module.socketio

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK] {name}")
    else:
        FAIL += 1
        print(f"  [NG] {name}")


def drain(client):
    """受信イベントを {イベント名: [args, ...]} に整形して返す。"""
    out = {}
    for pkt in client.get_received():
        out.setdefault(pkt["name"], []).append(
            pkt["args"][0] if pkt["args"] else {})
    return out


def wait_for(client, event, timeout=5.0):
    """指定イベントが届くまでポーリングする (バックグラウンドタスク対策)。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        got = drain(client)
        if event in got:
            return got[event][-1]
        time.sleep(0.1)
    return None


def db_row(sql, args=()):
    with db._conn() as conn:
        return conn.execute(sql, args).fetchone()


def db_rows(sql, args=()):
    with db._conn() as conn:
        return conn.execute(sql, args).fetchall()


# ============================================================
print("1. スキーマ検証")
with db._conn() as conn:
    game_cols = {r[1] for r in conn.execute("PRAGMA table_info(games)")}
    msg_cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
for col in ("ground_truth_identity", "backfilled", "queue_wait_ms",
            "ai_switch_deadline_ms", "prompt_version",
            "delay_model_version", "assignment_mode", "debriefed"):
    check(f"games.{col}", col in game_cols)
for col in ("char_count", "compose_time_ms", "displayed_delay_ms"):
    check(f"messages.{col}", col in msg_cols)

# ============================================================
print("2. 人対人 (force=human ×2)")
c1 = socketio.test_client(app)
c2 = socketio.test_client(app)
c1.emit("join_quick", {"force": "human"})
time.sleep(0.2)
c2.emit("join_quick", {"force": "human"})
time.sleep(0.3)

m1 = wait_for(c1, "match_found", 2)
m2 = wait_for(c2, "match_found", 2)
check("両者に match_found", m1 is not None and m2 is not None)
roles = {m1["role"], m2["role"]}
check("役割は asker と answerer", roles == {"asker", "answerer"})

asker, answerer = (c1, c2) if m1["role"] == "asker" else (c2, c1)
for turn in range(app_module.MAX_TURNS):
    asker.emit("submit_question",
               {"text": f"質問{turn+1}です", "compose_time_ms": 4000 + turn})
    q = wait_for(answerer, "question", 2)
    check(f"回答者が質問{turn+1}を受信", q is not None)
    answerer.emit("submit_answer",
                  {"text": f"回答{turn+1}だよ", "compose_time_ms": 6000 + turn})
    a = wait_for(asker, "answer", 2)
    check(f"質問者が回答{turn+1}を受信", a is not None)
check("最終ターンで can_judge", a and a.get("can_judge") is True)

asker.emit("submit_judge", {"guess": "human"})
r1 = wait_for(asker, "result", 2)
r2 = wait_for(answerer, "result", 2)
check("両者に result", r1 is not None and r2 is not None)
check("人間判定は正解", r1 and r1["correct"] is True)

g = db_row("SELECT * FROM games WHERE game_id=?", (r1["game_id"],))
check("ground_truth_identity='human'", g["ground_truth_identity"] == "human")
check("assignment_mode='forced'", g["assignment_mode"] == "forced")
check("backfilled=0", g["backfilled"] == 0)
msgs = db_rows("SELECT * FROM messages WHERE game_id=? ORDER BY id",
               (r1["game_id"],))
check("メッセージ10件保存", len(msgs) == app_module.MAX_TURNS * 2)
check("質問に compose_time_ms",
      all(m["compose_time_ms"] is not None for m in msgs
          if m["kind"] == "question"))
check("回答に compose_time_ms (人間)",
      all(m["compose_time_ms"] is not None for m in msgs
          if m["kind"] == "answer"))
check("全発言に char_count",
      all(m["char_count"] == len(m["content"]) for m in msgs))
c1.disconnect()
c2.disconnect()

# ============================================================
print("3. 強制AI戦 (force=ai)")
c3 = socketio.test_client(app)
c3.emit("join_quick", {"force": "ai"})
m3 = wait_for(c3, "match_found", 2)
check("即 match_found", m3 is not None)
check("人間は質問者固定 (b2)", m3 and m3["role"] == "asker")

for turn in range(app_module.MAX_TURNS):
    c3.emit("submit_question",
            {"text": f"AIへの質問{turn+1}", "compose_time_ms": 5000})
    a = wait_for(c3, "answer", 5)
    check(f"AI回答{turn+1}を受信", a is not None)

c3.emit("submit_judge", {"guess": "ai"})
r3 = wait_for(c3, "result", 2)
check("result 受信・AI判定は正解", r3 is not None and r3["correct"] is True)

g3 = db_row("SELECT * FROM games WHERE game_id=?", (r3["game_id"],))
check("ground_truth_identity='ai'", g3["ground_truth_identity"] == "ai")
check("assignment_mode='forced'", g3["assignment_mode"] == "forced")
check("prompt_version 記録", g3["prompt_version"] is not None)
check("delay_model_version 記録", g3["delay_model_version"] is not None)
ai_msgs = db_rows(
    "SELECT * FROM messages WHERE game_id=? AND kind='answer'",
    (r3["game_id"],))
check("AI回答に displayed_delay_ms",
      all(m["displayed_delay_ms"] is not None for m in ai_msgs))
check("AI回答に compose_time_ms なし",
      all(m["compose_time_ms"] is None for m in ai_msgs))
c3.disconnect()

# ============================================================
print("4. W切替 (αドロー・人間ドロー→タイムアウト→AI補填)")
app_module.ALPHA_AI = 0.0  # 必ず「人間」を引かせる (GAME_FAST_DELAYでW=0.5秒)
c4 = socketio.test_client(app)
c4.emit("join_quick", {})
m4 = wait_for(c4, "match_found", 5)  # 相手がいないのでW到達でAI戦へ
check("W到達でAI戦へ切替", m4 is not None and m4["role"] == "asker")

c4.emit("submit_question", {"text": "こんにちは", "compose_time_ms": 3000})
a4 = wait_for(c4, "answer", 5)
check("補填AIが応答", a4 is not None)
for turn in range(app_module.MAX_TURNS - 1):
    c4.emit("submit_question", {"text": f"追加質問{turn}", "compose_time_ms": 3000})
    wait_for(c4, "answer", 5)
c4.emit("submit_judge", {"guess": "human"})
r4 = wait_for(c4, "result", 2)
check("result 受信", r4 is not None)

g4 = db_row("SELECT * FROM games WHERE game_id=?", (r4["game_id"],))
check("backfilled=1 (沈黙補填の禁止)", g4["backfilled"] == 1)
check("assignment_mode='random'", g4["assignment_mode"] == "random")
check("ai_switch_deadline_ms 記録", g4["ai_switch_deadline_ms"] is not None)
check("queue_wait_ms 記録", g4["queue_wait_ms"] is not None)
c4.disconnect()

# ============================================================
print()
print(f"結果: {PASS} passed / {FAIL} failed")
if os.path.exists("test_game.db"):
    os.remove("test_game.db")
exit(0 if FAIL == 0 else 1)
