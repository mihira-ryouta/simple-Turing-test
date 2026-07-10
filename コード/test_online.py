"""
オンライン(クイックマッチ)の自己テスト。
相手も、ブラウザも不要。app.py に対して仮想の2クライアントを内部で走らせ、
マッチング〜質問〜回答〜判定〜切断通知までを自動で確認する。

実行:
    python test_online.py

注:
  - 実DB(database.py)とAI(ai_backend.py)には触れないよう、テスト内でダミーに
    差し替えている。研究用のSQLiteは汚さない。Geminiのキーも不要。
  - 「通信路そのもの」の確認(ブラウザで接続が張れるか)は、このテストとは別に、
    2タブで /公開 を押す方法で確認できる。
"""

import sys
import types

# --- app.py を読み込む前に、依存モジュールをダミーへ差し替える ---------------
# app.py は import 時に database.init_db() を呼ぶため、先に仕込む必要がある。

_fake_db = types.ModuleType("database")
_fake_db.CALLS = {"games": [], "players": [], "messages": []}
_fake_db._mid = [0]
_fake_db.init_db = lambda: None
_fake_db.save_game = lambda **kw: _fake_db.CALLS["games"].append(kw)


def _save_player(game_id, key, role=None, vote_target=None, correct=None):
    _fake_db.CALLS["players"].append(
        dict(game_id=game_id, key=key, role=role,
             vote_target=vote_target, correct=correct))


def _save_message(game_id, turn, speaker_id, kind, content, reply_to=None):
    _fake_db._mid[0] += 1
    _fake_db.CALLS["messages"].append(
        dict(id=_fake_db._mid[0], turn=turn, speaker_id=speaker_id,
             kind=kind, content=content, reply_to=reply_to))
    return _fake_db._mid[0]


_fake_db.save_player = _save_player
_fake_db.save_message = _save_message
_fake_db.save_survey = lambda **kw: None
_fake_db.save_annotation = lambda **kw: None

_fake_ai = types.ModuleType("ai_backend")
_fake_ai.generate_reply = lambda history: "(dummy)"

sys.modules["database"] = _fake_db
sys.modules["ai_backend"] = _fake_ai

# ここで初めて app を読み込む(上のダミーが使われる)
from app import app, socketio  # noqa: E402

db = _fake_db


def _names(recv):
    return [r["name"] for r in recv]


def run():
    c1 = socketio.test_client(app)
    c2 = socketio.test_client(app)
    assert c1.is_connected() and c2.is_connected(), "接続に失敗"
    print("[1] 2クライアントが接続できた")

    # 1人目 → 待機
    c1.emit("join_quick")
    r1 = c1.get_received()
    assert _names(r1) == ["waiting"], _names(r1)
    print("[2] 1人目は待機(waiting)になった")

    # 2人目 → マッチ成立、両者に役割が届く
    c2.emit("join_quick")
    r1 = c1.get_received()
    r2 = c2.get_received()
    assert "match_found" in _names(r1) and "match_found" in _names(r2)
    role1 = [r for r in r1 if r["name"] == "match_found"][0]["args"][0]["role"]
    role2 = [r for r in r2 if r["name"] == "match_found"][0]["args"][0]["role"]
    assert {role1, role2} == {"asker", "answerer"}, (role1, role2)
    print(f"[3] マッチ成立。役割割当 OK (c1={role1}, c2={role2})")

    asker, answerer = (c1, c2) if role1 == "asker" else (c2, c1)

    # 5往復の質問/回答が中継されるか
    for i in range(5):
        asker.emit("submit_question", {"text": f"Q{i+1}"})
        rq = answerer.get_received()
        assert "question" in _names(rq), (i, _names(rq))
        assert rq[-1]["args"][0]["text"] == f"Q{i+1}"

        answerer.emit("submit_answer", {"text": f"A{i+1}"})
        ra = asker.get_received()
        ans = [r for r in ra if r["name"] == "answer"][0]["args"][0]
        assert ans["text"] == f"A{i+1}" and ans["turn"] == i + 1
        assert ans["can_judge"] == (i + 1 >= 5)
    print("[4] 質問→回答の中継が5往復とも OK")

    # 判定 → 両者に結果
    asker.emit("submit_judge", {"guess": "human"})
    res_a = [r for r in asker.get_received() if r["name"] == "result"][0]["args"][0]
    res_b = [r for r in answerer.get_received() if r["name"] == "result"][0]["args"][0]
    assert res_a["correct"] is True and res_a["answer"] == "human"
    assert res_b["you_are"] == "answerer"
    print("[5] 判定と結果配信 OK (相手=人間、判定=正解)")

    # DB保存の中身
    g = db.CALLS["games"][-1]
    assert g["entry_type"] == "quick" and g["player_count"] == 2
    assert g["ai_present"] is False and g["ai_count"] == 0
    msgs = db.CALLS["messages"]
    assert sum(1 for m in msgs if m["kind"] == "question") == 5
    assert sum(1 for m in msgs if m["kind"] == "answer") == 5
    print("[6] DB保存の内容 OK (quick / 人間2人 / 質問5・回答5)")

    # 切断で相手に通知
    a2 = socketio.test_client(app)
    b2 = socketio.test_client(app)
    a2.emit("join_quick"); a2.get_received()
    b2.emit("join_quick"); a2.get_received(); b2.get_received()
    a2.disconnect()
    rb = b2.get_received()
    assert "opponent_left" in _names(rb), _names(rb)
    print("[7] 相手の切断通知(opponent_left) OK")

    print("\n=== オンライン経路: 全項目パス。通信ロジックは正常に動いています ===")


if __name__ == "__main__":
    run()
