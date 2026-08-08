# -*- coding: utf-8 -*-
"""
prompt_store.py ― システムプロンプト「本文」を外部ファイルから読み込む。

目的:
  ai_backend.py からプロンプト本文を切り離し、リポジトリに載せずに
  運用できるようにする(Render の Secret Files 等から供給する)。

置き場所(この順に探し、最初に見つかったものを使う):
  1. 環境変数 PROMPT_FILE で指定されたパス
  2. ./prompts.txt            (Render の非Docker サービスでは Secret File が
                               サービスのルートディレクトリにも置かれる)
  3. /etc/secrets/prompts.txt (Render の Secret File の絶対パス)

ファイル形式(1ファイルに全版を書く。エスケープ不要):

    === [v0] ===
    (本文が空 = システムプロンプトなし。素のモデル)

    === [v1] ===
    あなたはオンラインの雑談ゲームに参加している…

    === [rp-v1] ===
    あなたの役: {persona}

規約:
  - 見出し行 "=== [版名] ===" だけが特別扱い。それ以外は本文としてそのまま読む。
  - 本文が空白のみの版は None(=システムプロンプトなし)として扱う。
  - 最初の見出しより前の行は無視される(ファイル冒頭のコメント欄として使える)。

研究上の注意:
  本文をリポジトリ管理から外すと「どの版で何を投げたか」がコードから追跡できなく
  なる。そのため読み込み時に版ごとの SHA-256 を計算して公開する(digest())。
  ログや games テーブルにこのハッシュを残せば、あとから
  「v3 と記録された全ゲームが同一本文だった」ことを検証できる。
"""

import hashlib
import os
import re

# 見出し行: === [v1] ===   (前後の空白は許容)
_HEADER_RE = re.compile(r"^\s*===\s*\[(?P<name>[^\]]+)\]\s*===\s*$")

_DEFAULT_CANDIDATES = (
    "prompts.txt",
    "/etc/secrets/prompts.txt",
)


def resolve_path():
    """使用するプロンプトファイルのパスを返す。見つからなければ None。"""
    env_path = os.environ.get("PROMPT_FILE")
    if env_path:
        # 明示指定されたのに無い場合は候補探索に落とさない(取り違え防止)
        return env_path if os.path.isfile(env_path) else None
    for cand in _DEFAULT_CANDIDATES:
        if os.path.isfile(cand):
            return cand
    return None


def parse(text):
    """バンドル形式の文字列を {版名: 本文 or None} に変換する。"""
    versions = {}
    current = None
    buf = []

    def flush():
        if current is None:
            return
        body = "".join(buf)
        versions[current] = body.strip("\n") if body.strip() else None

    for line in text.splitlines(keepends=True):
        m = _HEADER_RE.match(line.rstrip("\n"))
        if m:
            flush()
            current = m.group("name").strip()
            if current in versions:
                raise ValueError(f"プロンプト版名が重複しています: {current}")
            buf = []
        elif current is not None:
            buf.append(line)
    flush()
    return versions


def load(path=None):
    """
    プロンプトファイルを読み込んで {版名: 本文 or None} を返す。
    ファイルが見つからない場合は FileNotFoundError を送出する
    (黙って空で起動すると、素のモデルでデータを取ってしまうため)。
    """
    path = path or resolve_path()
    if not path:
        raise FileNotFoundError(
            "プロンプトファイルが見つかりません。"
            "PROMPT_FILE を設定するか、./prompts.txt または "
            "/etc/secrets/prompts.txt を置いてください。"
        )
    with open(path, encoding="utf-8") as f:
        versions = parse(f.read())
    if not versions:
        raise ValueError(f"プロンプトファイルに版が1つもありません: {path}")
    return versions


def digest(body):
    """版の本文の SHA-256(先頭12桁)。本文なし(v0)は 'none'。"""
    if body is None:
        return "none"
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]
