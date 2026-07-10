# -*- coding: utf-8 -*-
"""
ゲーム記録のデータベース (SQLite) ― 計測対応版。決定メモ§4に対応。

研究用に5つのテーブルを持つ:
  games       … ゲーム単位 (構成と結果 + 実験条件・計測)
  players     … プレイヤー単位 (役割・投票・正誤)
  messages    … 会話ログ (質問と返し + タイピング計測)
  annotations … 発言への注釈 (どの発言が決め手/違和感か)
  surveys     … アンケート (ゲーム全体への任意回答)

アップグレードでの追加列 (どれも「後から遡って埋められない」ため最優先で追加):
  messages.char_count         … 本文の文字数 (全ターン)
  messages.compose_time_ms    … フォーカス→送信の実時間 (人間ターンのみ)
  messages.displayed_delay_ms … 挿入した応答遅延 (AIターンのみ)
  games.ground_truth_identity … 'ai' / 'human' (正体ドローの結果)
  games.backfilled            … 人間ドローだがAI補填した場合 1
  games.queue_wait_ms         … 実際の待ち時間 (質問者側)
  games.ai_switch_deadline_ms … 引いたW (クイックのみ)
  games.prompt_version        … 使用したAIプロンプトの版 (v0, v1, ...)
  games.delay_model_version   … 使用した遅延モデルの版
  games.assignment_mode       … 'random'(本番: αドロー) / 'forced'(プロトタイプ: 直接割当)
  games.debriefed             … 根源公開(手がかり開示)を見せた場合 1

init_db() は新規作成と既存DBの自動マイグレーションの両方を行う(冪等)。
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


# 既存DBへ足す列の一覧 (テーブル, 列名, 型と制約)。冪等マイグレーションに使う。
_MIGRATION_COLUMNS = [
    ("messages", "char_count",            "INTEGER"),
    ("messages", "compose_time_ms",       "INTEGER"),
    ("messages", "displayed_delay_ms",    "INTEGER"),
    ("games",    "ground_truth_identity", "TEXT"),
    ("games",    "backfilled",            "INTEGER DEFAULT 0"),
    ("games",    "queue_wait_ms",         "INTEGER"),
    ("games",    "ai_switch_deadline_ms", "INTEGER"),
    ("games",    "prompt_version",        "TEXT"),
    ("games",    "delay_model_version",   "TEXT"),
    ("games",    "assignment_mode",       "TEXT"),
    ("games",    "debriefed",             "INTEGER DEFAULT 0"),
]


def init_db():
    """テーブルを作成し、既存DBなら不足列を足す (どちらも冪等)。"""
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
            result       TEXT,
            ground_truth_identity TEXT,
            backfilled            INTEGER DEFAULT 0,
            queue_wait_ms         INTEGER,
            ai_switch_deadline_ms INTEGER,
            prompt_version        TEXT,
            delay_model_version   TEXT,
            assignment_mode       TEXT,
            debriefed             INTEGER DEFAULT 0
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
            char_count         INTEGER,     -- 本文の文字数 (全ターン)
            compose_time_ms    INTEGER,     -- フォーカス→送信の実時間 (人間のみ)
            displayed_delay_ms INTEGER,     -- 挿入した応答遅延 (AIのみ)
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
        # --- 既存DBの自動マイグレーション (冪等) ---
        for table, col, decl in _MIGRATION_COLUMNS:
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def _now():
    return datetime.now(timezone.utc).isoformat()


def save_game(game_id, entry_type, player_count, ai_present,
              ai_count, ai_model, result,
              ground_truth_identity=None, backfilled=False,
              queue_wait_ms=None, ai_switch_deadline_ms=None,
              prompt_version=None, delay_model_version=None,
              assignment_mode=None, debriefed=False):
    with _conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO games
               (game_id, created_at, entry_type, player_count,
                ai_present, ai_count, ai_model, result,
                ground_truth_identity, backfilled, queue_wait_ms,
                ai_switch_deadline_ms, prompt_version, delay_model_version,
                assignment_mode, debriefed)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (game_id, _now(), entry_type, player_count,
             int(ai_present), ai_count, ai_model, result,
             ground_truth_identity, int(backfilled), queue_wait_ms,
             ai_switch_deadline_ms, prompt_version, delay_model_version,
             assignment_mode, int(debriefed)),
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


def save_message(game_id, turn, speaker_id, kind, content, reply_to=None,
                 char_count=None, compose_time_ms=None,
                 displayed_delay_ms=None):
    """
    発言を保存し、その行のid(message_id)を返す。
    回答を保存するときは reply_to に元の質問のidを渡すと、質問と回答が紐づく。
    char_count は省略時に len(content) で自動計算する。
    """
    if char_count is None and content is not None:
        char_count = len(content)
    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO messages
               (game_id, turn, speaker_id, kind, reply_to, content, created_at,
                char_count, compose_time_ms, displayed_delay_ms)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (game_id, turn, speaker_id, kind, reply_to, content, _now(),
             char_count, compose_time_ms, displayed_delay_ms),
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


def mark_debriefed(game_id):
    """根源公開(手がかり開示)を見せたゲームに印を付ける (決定メモ§8)。"""
    with _conn() as conn:
        conn.execute(
            "UPDATE games SET debriefed=1 WHERE game_id=?", (game_id,))


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
