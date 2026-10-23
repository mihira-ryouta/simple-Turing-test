# -*- coding: utf-8 -*-
"""
import_exports.py ― Webから取ってきた /admin/export の JSON を1つのSQLiteにまとめる。

背景:
  Render 無料枠はファイルシステムが揮発するため、データはブラウザから
  /admin/export を開いて手元に保存する運用になっている。
  この方式には2つ問題があり、それを解消するのがこのスクリプト。

  問題1: エクスポートは「その時点の全件スナップショット」なので、
         複数回取ると内容が重複する。単純に連結すると同じゲームが二重に数えられる。
  問題2: messages.id は AUTOINCREMENT の連番で、DBが消えて作り直されるたびに
         1から振り直される。別々のエクスポートを連結すると id が衝突し、
         annotations.message_id の参照先が別のゲームの発言にずれる。

対処:
  - ゲーム単位で重複排除する(同じ game_id が複数ファイルにあれば、
    後のファイル= より完全な方を採用)
  - 発言 id をファイルごとにオフセットして振り直し、
    messages.reply_to と annotations.message_id も同時に付け替える

使い方:
    # 1. ブラウザで /admin/export?token=... を開き、中身を保存する
    #    保存先: analysis/data/export_2026-08-08.json のように日付を付ける
    # 2. まとめる
    python import_exports.py                    # analysis/data/*.json → merged.db
    python import_exports.py --data-dir data --out merged.db
    # 3. ノートブックの DB_PATH を "merged.db" にする

コピー&ペーストの注意:
  - Firefox でJSONを開くと整形ビューが出る。「Raw Data」タブから
    コピーしないと壊れた文字列になる。Chrome/Safari はそのままで良い。
  - ファイル末尾が "}" で終わっているか必ず確認する(途中で切れていると
    このスクリプトが JSONDecodeError で止まる。止まる方が安全)。
  - 保存時の文字コードは UTF-8。
  - そもそも curl で取るほうが確実:
      curl "https://<host>/admin/export?token=<TOKEN>" -o data/export_2026-08-08.json
"""

import argparse
import glob
import json
import os
import sqlite3
import sys
from collections import defaultdict

TABLES = ("games", "players", "messages", "annotations", "surveys", "votes")


def read_files(paths):
    """[(パス, データ)] を返す。壊れたファイルはそこで止める(黙って捨てない)。"""
    out = []
    for p in paths:
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            raise SystemExit(
                f"[中断] {p} が JSON として読めません: {e}\n"
                "  コピー&ペーストで途中が欠けている可能性があります。"
                "Firefox なら「Raw Data」タブからコピーし直すか、"
                "curl で取り直してください。")
        missing = [t for t in TABLES if t not in data]
        if missing:
            print(f"  [警告] {os.path.basename(p)}: テーブルが無い {missing}")
        out.append((p, data))
    return out


def merge(files):
    """
    ゲーム単位で重複排除しつつ、発言idを振り直して結合する。
    戻り値: {テーブル名: [行, ...]}, 統計情報
    """
    # 1) 各 game_id をどのファイルから採るか決める(後のファイルを優先)
    owner = {}
    for path, data in files:
        for g in data.get("games", []):
            owner[g["game_id"]] = path

    merged = {t: [] for t in TABLES}
    id_offset = 0
    stats = {"files": len(files), "dup_games": 0, "remapped": 0}
    seen_games = set()

    for path, data in files:
        # このファイルが担当するゲームだけ採用する
        mine = {g["game_id"] for g in data.get("games", [])
                if owner.get(g["game_id"]) == path}
        skipped = {g["game_id"] for g in data.get("games", [])} - mine
        stats["dup_games"] += len(skipped)

        # 発言idの対応表 (旧id → 新id)
        idmap = {}
        local_max = 0
        for m in data.get("messages", []):
            if m.get("id") is not None:
                local_max = max(local_max, int(m["id"]))
        for m in data.get("messages", []):
            if m["game_id"] in mine and m.get("id") is not None:
                idmap[int(m["id"])] = id_offset + int(m["id"])
        if idmap:
            stats["remapped"] += len(idmap)

        for g in data.get("games", []):
            if g["game_id"] in mine:
                merged["games"].append(dict(g))
                seen_games.add(g["game_id"])

        for t in ("players", "surveys", "votes"):
            for r in data.get(t, []):
                if r["game_id"] in mine:
                    merged[t].append(dict(r))

        for m in data.get("messages", []):
            if m["game_id"] not in mine:
                continue
            r = dict(m)
            r["id"] = idmap.get(int(m["id"])) if m.get("id") is not None else None
            if r.get("reply_to") is not None:
                r["reply_to"] = idmap.get(int(m["reply_to"]))
            merged["messages"].append(r)

        for a in data.get("annotations", []):
            if a["game_id"] not in mine:
                continue
            r = dict(a)
            old = a.get("message_id")
            r["message_id"] = idmap.get(int(old)) if old is not None else None
            if old is not None and r["message_id"] is None:
                # 参照先が見つからない注釈。捨てずに残すが印を付ける
                r["message_id"] = None
                r["free_text"] = (r.get("free_text") or "") + "  [参照先不明]"
            merged["annotations"].append(r)

        id_offset += local_max + 1

    stats["games"] = len(seen_games)
    return merged, stats


def write_db(merged, out_path, schema_from=None):
    """database.py の init_db() でスキーマを作り、行を流し込む。"""
    if os.path.exists(out_path):
        os.remove(out_path)
    sys.path.insert(0, schema_from or os.path.dirname(os.path.abspath(__file__)))
    os.environ["GAME_DB_PATH"] = out_path
    try:
        import database
        database.DB_PATH = out_path
        database.init_db()
    except ImportError:
        raise SystemExit(
            "[中断] database.py が読めません。--repo でリポジトリの場所を指定してください。")

    conn = sqlite3.connect(out_path)
    conn.execute("PRAGMA foreign_keys = OFF")   # 挿入順に依存しないようにする
    for t in TABLES:
        rows = merged[t]
        if not rows:
            continue
        cols = [c[1] for c in conn.execute(f"PRAGMA table_info({t})")]
        use = [c for c in cols if any(c in r for r in rows)]
        ph = ",".join("?" * len(use))
        conn.executemany(
            f"INSERT INTO {t} ({','.join(use)}) VALUES ({ph})",
            [tuple(r.get(c) for c in use) for r in rows])
    conn.commit()
    conn.close()


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="エクスポートJSONをまとめて1つのSQLiteにする")
    ap.add_argument("--data-dir", default="data",
                    help="JSONの置き場 (既定: data)")
    ap.add_argument("--pattern", default="*.json")
    ap.add_argument("--out", default="merged.db")
    ap.add_argument("--repo", default="..",
                    help="database.py があるディレクトリ (既定: ..)")
    args = ap.parse_args(argv)

    paths = sorted(glob.glob(os.path.join(args.data_dir, args.pattern)))
    if not paths:
        raise SystemExit(
            f"[中断] {args.data_dir}/{args.pattern} にファイルがありません。\n"
            "  ブラウザで /admin/export?token=... を開いて保存するか、\n"
            "  curl \"https://<host>/admin/export?token=<TOKEN>\" "
            "-o data/export_YYYY-MM-DD.json")

    print(f"読み込み {len(paths)} ファイル:")
    for p in paths:
        print(f"  - {os.path.basename(p)}")
    files = read_files(paths)
    merged, stats = merge(files)

    print("\n結合結果:")
    for t in TABLES:
        print(f"  {t:<12}{len(merged[t]):>6} 行")
    print(f"  重複により捨てたゲーム: {stats['dup_games']}")
    print(f"  振り直した発言id      : {stats['remapped']}")

    # 整合性の確認 (振り直しが正しくできたか)
    msg_game = {m["id"]: m["game_id"] for m in merged["messages"]
                if m.get("id") is not None}
    dup_ids = len(merged["messages"]) - len(msg_game)
    bad_reply = sum(1 for m in merged["messages"]
                    if m.get("reply_to") is not None
                    and m["reply_to"] not in msg_game)
    lost_ann = sum(1 for a in merged["annotations"] if a.get("message_id") is None)
    cross_ann = sum(1 for a in merged["annotations"]
                    if a.get("message_id") is not None
                    and msg_game.get(a["message_id"]) != a["game_id"])
    print("\n整合性:")
    print(f"  発言idの重複          : {dup_ids}")
    print(f"  reply_to の参照切れ   : {bad_reply}")
    print(f"  参照先を失った注釈    : {lost_ann}")
    print(f"  ゲームをまたぐ注釈    : {cross_ann}"
          + ("  ← 元データの時点で壊れている疑い" if cross_ann else ""))

    write_db(merged, args.out, schema_from=os.path.abspath(args.repo))
    print(f"\n→ {args.out} を作成しました。"
          f"ノートブックの DB_PATH を \"{args.out}\" にしてください。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
