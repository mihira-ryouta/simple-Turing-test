"""
ゲーム記録のデータベース (SQLite) ― 注釈対応版。

研究用に5つのテーブルを持つ:
  games       … ゲーム単位 (構成と結果)
  players     … プレイヤー単位 (役割・投票・正誤)
  messages    … 会話ログ (質問と返し、reply_toで質問と回答を紐付け)
  annotations … 発言への注釈 (どの発言が決め手/違和感か) ★今回追加
  surveys     … アンケート (ゲーム全体への任意回答)

すべて game_id で繋がり、annotations は message_id で個々の発言に紐づく。
これにより次の3つを1つの構造で分析できる:
  - AIのどんな発言が見破られやすいか (messages×annotations)
  - 人間は何を根拠に判断するか      (annotations.kind / surveys.basis)
  - どんな質問がAIをあぶり出すか      (messages.kind=question × reply_toの回答への注釈)
"""

import sqlite3
import json
import csv
import os
from datetime import datetime, timezone

DB_PATH = os.environ.get("GAME_DB_PATH", "game_data.db")


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """テーブルを作成する (既にあれば何もしない)。"""
    with _conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS games (
            game_id      TEXT PRIMARY KEY,
            created_at   TEXT NOT NULL,
            entry_type   TEXT,
            player_count INTEGER,
            ai_present   INTEGER,
            ai_count     INTEGER,
            ai_model     TEXT,
            result       TEXT
        );

        CREATE TABLE IF NOT EXISTS players (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id     TEXT NOT NULL,
            player_id   TEXT,               -- 各プレイヤー固有のID
            role        TEXT,               -- human / ai / human_as_ai
            vote_target TEXT,               -- 投票先の player_id
            correct     INTEGER,            -- 投票が当たったか 0/1 (AIはNULL)
            FOREIGN KEY (game_id) REFERENCES games(game_id)
        );

        CREATE TABLE IF NOT EXISTS messages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id    TEXT NOT NULL,
            turn       INTEGER,
            speaker_id TEXT,                -- 発言者の player_id
            kind       TEXT,                -- question / answer
            reply_to   INTEGER,             -- 回答のとき、元の質問メッセージのid
            content    TEXT,
            created_at TEXT,
            FOREIGN KEY (game_id)  REFERENCES games(game_id),
            FOREIGN KEY (reply_to) REFERENCES messages(id)
        );

        CREATE TABLE IF NOT EXISTS annotations (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id      TEXT NOT NULL,
            message_id   INTEGER NOT NULL,  -- どの発言への注釈か
            annotator_id TEXT,              -- 注釈したプレイヤーのID
            kind         TEXT,              -- decisive(決め手) / suspicious(違和感) / natural(自然) など
            free_text    TEXT,              -- 「ここがこう違った」の自由記述
            FOREIGN KEY (game_id)    REFERENCES games(game_id),
            FOREIGN KEY (message_id) REFERENCES messages(id)
        );

        CREATE TABLE IF NOT EXISTS surveys (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id       TEXT NOT NULL,
            respondent_id TEXT,
            confidence    INTEGER,
            basis         TEXT,
            free_text     TEXT,
            FOREIGN KEY (game_id) REFERENCES games(game_id)
        );
        """)


def _now():
    return datetime.now(timezone.utc).isoformat()


def save_game(game_id, entry_type, player_count, ai_present,
              ai_count, ai_model, result):
    with _conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO games
               (game_id, created_at, entry_type, player_count,
                ai_present, ai_count, ai_model, result)
               VALUES (?,?,?,?,?,?,?,?)""",
            (game_id, _now(), entry_type, player_count,
             int(ai_present), ai_count, ai_model, result),
        )


def save_player(game_id, player_id, role, vote_target, correct):
    with _conn() as conn:
        conn.execute(
            """INSERT INTO players
               (game_id, player_id, role, vote_target, correct)
               VALUES (?,?,?,?,?)""",
            (game_id, player_id, role, vote_target,
             None if correct is None else int(correct)),
        )


def save_message(game_id, turn, speaker_id, kind, content, reply_to=None):
    """
    発言を保存し、その行のid(message_id)を返す。
    回答を保存するときは reply_to に元の質問のidを渡すと、質問と回答が紐づく。
    """
    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO messages
               (game_id, turn, speaker_id, kind, reply_to, content, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (game_id, turn, speaker_id, kind, reply_to, content, _now()),
        )
        return cur.lastrowid


def save_annotation(game_id, message_id, annotator_id, kind, free_text=""):
    """特定の発言への注釈を保存する (決め手/違和感 など)。"""
    with _conn() as conn:
        conn.execute(
            """INSERT INTO annotations
               (game_id, message_id, annotator_id, kind, free_text)
               VALUES (?,?,?,?,?)""",
            (game_id, message_id, annotator_id, kind, free_text),
        )


def save_survey(game_id, respondent_id, confidence, basis, free_text):
    """アンケートは任意。未回答ならこの関数を呼ばなければよい。"""
    with _conn() as conn:
        conn.execute(
            """INSERT INTO surveys
               (game_id, respondent_id, confidence, basis, free_text)
               VALUES (?,?,?,?,?)""",
            (game_id, respondent_id, confidence, basis, free_text),
        )


# ---- 書き出し ----

def export_json(path="export.json"):
    out = {}
    with _conn() as conn:
        for table in ("games", "players", "messages", "annotations", "surveys"):
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()
            out[table] = [dict(r) for r in rows]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    return path


def export_csv(table, path=None):
    path = path or f"{table}.csv"
    with _conn() as conn:
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        cols = [c[1] for c in conn.execute(f"PRAGMA table_info({table})")]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(cols)
        for r in rows:
            writer.writerow([r[c] for c in cols])
    return path


if __name__ == "__main__":
    init_db()
    print(f"DB初期化完了: {DB_PATH}")
