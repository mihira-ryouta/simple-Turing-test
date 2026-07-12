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
print("5. プライベートマッチ (humanモード)")
h1 = socketio.test_client(app)
g1 = socketio.test_client(app)
h1.emit("create_private", {"key": "neko123", "mode": "human"})
created = wait_for(h1, "private_created", 2)
check("private_created 受信", created is not None and created["key"] == "neko123")

# 誤った合言葉では入れない
bad = socketio.test_client(app)
bad.emit("join_private", {"key": "inu999"})
err = wait_for(bad, "private_error", 2)
check("誤合言葉は private_error", err is not None)
bad.disconnect()

# 同じ合言葉の二重作成は拒否
dup = socketio.test_client(app)
dup.emit("create_private", {"key": "neko123", "mode": "human"})
err2 = wait_for(dup, "private_error", 2)
check("合言葉の重複作成は拒否", err2 is not None)
dup.disconnect()

g1.emit("join_private", {"key": "neko123"})
mh = wait_for(h1, "match_found", 2)
mg = wait_for(g1, "match_found", 2)
check("両者に match_found", mh is not None and mg is not None)
check("役割は asker/answerer", {mh["role"], mg["role"]} == {"asker", "answerer"})

p_asker, p_answerer = (h1, g1) if mh["role"] == "asker" else (g1, h1)
for turn in range(app_module.MAX_TURNS):
    p_asker.emit("submit_question",
                 {"text": f"P質問{turn+1}", "compose_time_ms": 3000})
    assert wait_for(p_answerer, "question", 2) is not None
    p_answerer.emit("submit_answer",
                    {"text": f"P回答{turn+1}", "compose_time_ms": 4000})
    a = wait_for(p_asker, "answer", 2)
check("5ターン完走", a is not None and a.get("can_judge") is True)
p_asker.emit("submit_judge", {"guess": "human"})
rp = wait_for(p_asker, "result", 2)
check("判定は正解 (human)", rp is not None and rp["correct"] is True)
gp = db_row("SELECT * FROM games WHERE game_id=?", (rp["game_id"],))
check("entry_type='private'", gp["entry_type"] == "private")
check("assignment_mode='private_human'", gp["assignment_mode"] == "private_human")
h1.disconnect()
g1.disconnect()

# ============================================================
print("6. プライベートマッチ (shuffleモード・AI側)")
app_module.PRIVATE_SHUFFLE_AI_P = 1.0  # 必ずAIを引かせる
h2 = socketio.test_client(app)
g2 = socketio.test_client(app)
h2.emit("create_private", {"key": "tori77", "mode": "shuffle"})
wait_for(h2, "private_created", 2)
g2.emit("join_private", {"key": "tori77"})
mh2 = wait_for(h2, "match_found", 2)
mg2 = wait_for(g2, "match_found", 2)
check("ホストは観戦者", mh2 is not None and mh2["role"] == "spectator")
check("ゲストは質問者", mg2 is not None and mg2["role"] == "asker")

for turn in range(app_module.MAX_TURNS):
    g2.emit("submit_question",
            {"text": f"S質問{turn+1}", "compose_time_ms": 3500})
    a2 = wait_for(g2, "answer", 5)
    check(f"AI回答{turn+1}をゲストが受信", a2 is not None)
sp = wait_for(h2, "spect_message", 2)
check("観戦者に会話が中継される", sp is not None)

g2.emit("submit_judge", {"guess": "ai"})
rg = wait_for(g2, "result", 2)
rh = wait_for(h2, "result", 2)
check("ゲストに result", rg is not None and rg["correct"] is True)
check("観戦者にも result", rh is not None and rh.get("you_are") == "spectator")
gs = db_row("SELECT * FROM games WHERE game_id=?", (rg["game_id"],))
check("entry_type='private'", gs["entry_type"] == "private")
check("assignment_mode='private_shuffle'", gs["assignment_mode"] == "private_shuffle")
check("ground_truth='ai'", gs["ground_truth_identity"] == "ai")
check("prompt_version 記録", gs["prompt_version"] is not None)
h2.disconnect()
g2.disconnect()

# ============================================================
print("7. プライベートマッチ (shuffleモード・人間側)")
app_module.PRIVATE_SHUFFLE_AI_P = 0.0  # 必ずホスト(人間)を引かせる
h3 = socketio.test_client(app)
g3 = socketio.test_client(app)
h3.emit("create_private", {"key": "kuma5", "mode": "shuffle"})
wait_for(h3, "private_created", 2)
g3.emit("join_private", {"key": "kuma5"})
mh3 = wait_for(h3, "match_found", 2)
mg3 = wait_for(g3, "match_found", 2)
check("ホストは回答者", mh3 is not None and mh3["role"] == "answerer")
check("ゲストは質問者", mg3 is not None and mg3["role"] == "asker")
g3.emit("submit_question", {"text": "友達?", "compose_time_ms": 2000})
check("ホストが質問受信", wait_for(h3, "question", 2) is not None)
h3.emit("submit_answer", {"text": "さあね", "compose_time_ms": 3000})
check("ゲストが回答受信", wait_for(g3, "answer", 2) is not None)
h3.disconnect()
g3.disconnect()

# ============================================================
print("8. グループ戦 (人間3 + AI1 + AIのふり1・配役あり)")
gh = socketio.test_client(app)   # ホスト
ga = socketio.test_client(app)   # ゲスト1
gb = socketio.test_client(app)   # ゲスト2
gh.emit("create_group", {"key": "mura8", "humans": 3, "ai_count": 1,
                         "human_as_ai": 1, "roleplay": True})
lb = wait_for(gh, "group_lobby", 2)
check("ロビー作成 (1/3)", lb is not None and lb["joined"] == 1)
ga.emit("join_private", {"key": "mura8"})
lb2 = wait_for(gh, "group_lobby", 2)
check("ロビー更新 (2/3)", lb2 is not None and lb2["joined"] == 2)
gb.emit("join_private", {"key": "mura8"})
time.sleep(0.3)

clients = {"host": gh, "a": ga, "b": gb}
starts = {}
for name, c in clients.items():
    starts[name] = wait_for(c, "group_started", 3)
check("全員に group_started", all(s is not None for s in starts.values()))
judges = [n for n, s in starts.items() if s["role"] == "judge"]
answerers = [n for n, s in starts.items() if s["role"] == "answerer"]
check("判定者1人・回答者2人(人間)", len(judges) == 1 and len(answerers) == 2)
check("判定者のパネルは3枠 (人間2+AI1)",
      len(starts[judges[0]]["aliases"]) == 3)
check("AIのふり役が1人",
      sum(1 for n in answerers if starts[n]["act_as_ai"]) == 1)
check("配役が全回答者に配布",
      all(starts[n]["persona"] for n in answerers))

judge_c = clients[judges[0]]
for turn in range(app_module.MAX_TURNS):
    judge_c.emit("submit_group_question",
                 {"text": f"G質問{turn+1}", "compose_time_ms": 3000})
    for n in answerers:
        q = wait_for(clients[n], "group_question", 3)
        assert q is not None, f"{n} が質問{turn+1}を未受信"
        clients[n].emit("submit_group_answer",
                        {"text": f"{n}の回答{turn+1}", "compose_time_ms": 4000})
    tc = wait_for(judge_c, "group_turn_complete", 6)
    check(f"ターン{turn+1}完了 (AI含む全回答が揃う)", tc is not None)
check("最終ターンで can_judge", tc and tc.get("can_judge") is True)

# 全部 "human" とラベル → AI1体ぶんだけ不正解 = 2/3
aliases = starts[judges[0]]["aliases"]
judge_c.emit("submit_group_judgement",
             {"labels": {a: "human" for a in aliases}})
rj = wait_for(judge_c, "group_result", 3)
check("判定者に group_result", rj is not None and rj["you_are"] == "judge")
check("スコア 2/3 (AIのみ外す)", rj and rj["score"] == 2 and rj["total"] == 3)
ra = [wait_for(clients[n], "group_result", 3) for n in answerers]
check("回答者にも group_result", all(r is not None for r in ra))
haa_res = next(r for r in ra if r["your_kind"] == "human_as_ai")
check("AIのふり役: humanと判定され敗北", haa_res["you_won"] is False)

gg = db_row("SELECT * FROM games WHERE game_id=?", (rj["game_id"],))
check("entry_type='private_group'", gg["entry_type"] == "private_group")
check("result='2/3'", gg["result"] == "2/3")
check("prompt_version='rp-v1' (配役)", gg["prompt_version"] == "rp-v1")
prs = db_rows("SELECT * FROM players WHERE game_id=?", (rj["game_id"],))
check("players 4行 (判定者+パネル3)", len(prs) == 4)
roles = sorted(p["role"] for p in prs)
check("役割构成 human×2, ai, human_as_ai",
      roles == ["ai", "human", "human", "human_as_ai"])
check("パネルに judged_as 記録",
      all(p["judged_as"] == "human" for p in prs if p["alias"] != "判定者"))
check("配役 persona 記録",
      all(p["persona"] for p in prs if p["role"] != "human" or p["alias"] != "判定者"))
gmsgs = db_rows("SELECT * FROM messages WHERE game_id=? ORDER BY id",
                (rj["game_id"],))
check("メッセージ 5質問+15回答",
      sum(1 for m in gmsgs if m["kind"] == "question") == 5
      and sum(1 for m in gmsgs if m["kind"] == "answer") == 15)
check("AI回答に displayed_delay_ms",
      all(m["displayed_delay_ms"] is not None for m in gmsgs
          if m["kind"] == "answer" and m["compose_time_ms"] is None))
gh.disconnect(); ga.disconnect(); gb.disconnect()

# ============================================================
print("9. グループ戦の異常系")
e1 = socketio.test_client(app)
e1.emit("create_group", {"key": "solo", "humans": 2, "ai_count": 0,
                         "human_as_ai": 0, "roleplay": False})
wait_for(e1, "group_lobby", 2)
e1.emit("start_group", {"key": "solo"})
err_solo = wait_for(e1, "private_error", 2)
check("1人では開始不可", err_solo is not None)
e2 = socketio.test_client(app)
e2.emit("create_group", {"key": "solo", "humans": 2, "ai_count": 1,
                         "human_as_ai": 0})
errd = wait_for(e2, "private_error", 2)
check("グループでも合言葉の重複は拒否", errd is not None)
e1.disconnect(); e2.disconnect()

# ============================================================
print()
print(f"結果: {PASS} passed / {FAIL} failed")
if os.path.exists("test_game.db"):
    os.remove("test_game.db")
exit(0 if FAIL == 0 else 1)
