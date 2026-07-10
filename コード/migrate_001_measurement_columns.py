# -*- coding: utf-8 -*-
"""
migrate_001_measurement_columns.py
決定メモ§4のスキーマ変更を適用するマイグレーション。

- 冪等: 既に列が存在する場合はスキップするので何度実行しても安全
- 既存データは保持される（ALTER TABLE ADD COLUMN のみ使用）
- 実行前に DB ファイルのバックアップを自動作成

使い方:
    python migrate_001_measurement_columns.py [DBパス]
    （省略時は ./game.db。実際のDBファイル名に合わせて DEFAULT_DB を変更すること）
"""

import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_DB = "game.db"  # ← 実際のDBファイル名に合わせて変更

# (テーブル名, 列名, 型と制約)
COLUMNS = [
    # --- messages: ターン単位の計測 ---
    ("messages", "char_count",         "INTEGER"),          # 本文の文字数（全ターン）
    ("messages", "compose_time_ms",    "INTEGER"),          # フォーカス→送信の実時間（人間ターンのみ）
    ("messages", "displayed_delay_ms", "INTEGER"),          # 挿入した応答遅延（AIターンのみ）
    # --- games: ゲーム単位の条件・結果 ---
    ("games", "ground_truth_identity", "TEXT"),             # 'ai' / 'human'
    ("games", "backfilled",            "INTEGER DEFAULT 0"),# 人間ドロー→AI補填なら1
    ("games", "queue_wait_ms",         "INTEGER"),          # 実際の待ち時間
    ("games", "ai_switch_deadline_ms", "INTEGER"),          # 引いたW
    ("games", "prompt_version",        "TEXT"),             # 例: 'v0'
    ("games", "delay_model_version",   "TEXT"),             # バージョン名 or パラメータJSON
    ("games", "debriefed",             "INTEGER DEFAULT 0"),# 根源公開を見せたら1
]


def existing_columns(conn: sqlite3.Connection, table: str) -> set:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def main() -> int:
    db_path = Path(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
    if not db_path.exists():
        print(f"[ERROR] DBが見つかりません: {db_path}")
        print("        パスを引数で指定するか DEFAULT_DB を修正してください。")
        return 1

    # バックアップ（タイムスタンプ付き）
    backup = db_path.with_suffix(
        db_path.suffix + f".bak.{datetime.now():%Y%m%d_%H%M%S}"
    )
    shutil.copy2(db_path, backup)
    print(f"[OK] バックアップ作成: {backup}")

    conn = sqlite3.connect(db_path)
    try:
        added, skipped = [], []
        for table, col, decl in COLUMNS:
            if not table_exists(conn, table):
                print(f"[ERROR] テーブルが存在しません: {table}")
                print("        テーブル名が異なる場合は COLUMNS を修正してください。")
                return 1
            if col in existing_columns(conn, table):
                skipped.append(f"{table}.{col}")
                continue
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            added.append(f"{table}.{col}")
        conn.commit()

        print(f"[OK] 追加した列 ({len(added)}件):")
        for c in added:
            print(f"     + {c}")
        if skipped:
            print(f"[SKIP] 既に存在 ({len(skipped)}件): {', '.join(skipped)}")

        # 検証: 追加後の全列を表示
        for table in ("messages", "games"):
            cols = sorted(existing_columns(conn, table))
            print(f"[VERIFY] {table}: {', '.join(cols)}")
    finally:
        conn.close()

    print("[DONE] マイグレーション完了")
    return 0


if __name__ == "__main__":
    sys.exit(main())
