# -*- coding: utf-8 -*-
"""
ai_backend.py ― AI応答の差し替えポイント + プロンプトのバージョン管理。

変更点(アップグレード版):
  1. プロンプトを「バージョン」として登録制にした (決定D7)。
       v0 = 素のGemini(システムプロンプトなし)。検出されやすさのベースライン。
       v1 = 従来のペルソナプロンプト。
     どのゲームでどの版を使ったかは games.prompt_version に必ず記録される。
     改訂するときは既存の版を書き換えず、v2, v3... と新しい版を足すこと
     (書き換えると過去データとの対応が壊れる)。
  2. 簡易レート制限を追加。無料枠のRPM制限(概ね15RPM=4秒に1回)への保険として、
     Gemini呼び出しの間隔を最低 MIN_CALL_INTERVAL_S 空ける。
     失敗時は1回だけリトライし、それでもだめならダミー応答に落ちる。

使い方:
  1. pip install google-genai
  2. https://aistudio.google.com/apikey でAPIキーを取得
  3. export GEMINI_API_KEY="あなたのキー"
  APIキーが無い/失敗した場合は自動でダミー応答にフォールバック。
"""

import os
import random
import threading
import time

# 使うモデル名。app.py 側はこの値を参照して記録する(値の二重管理をしない)。
MODEL_NAME = "gemini-3.5-flash"

# ============================================================
# プロンプトのバージョン登録簿 (決定D7)
#   None = システムプロンプトなし(素のGemini) = v0
#   改訂は「追加」で行い、過去の版は残す。
# ============================================================
PROMPT_VERSIONS = {
    "v0": None,  # 素のGemini。検出されやすさの天井を測るベースライン。
    "v1": """あなたはオンラインの雑談ゲームに参加している一人の人間プレイヤーです。
相手はあなたが人間かAIかを当てようとしています。

ルール:
- あなたは普通の人間のフリをして、質問に自然に答えてください。
- 答えは短め(1〜3文)で、口語的に。完璧な敬語や説明口調は避ける。
- 「AIとして」とか「私は人間です」とは絶対に言わない。
- 知らないことは適当にごまかしたり、「わからん」と言ってもいい。
- たまに脱線したり、感情(面倒くさい、笑う、など)を出してもいい。
- 長い箇条書きや丁寧すぎる説明はしない。人間はそんな喋り方をしない。
""",
}

# いま使う版。実験フェーズごとにここを切り替える(ゲーム内では固定される: 決定D8)。
DEFAULT_PROMPT_VERSION = os.environ.get("GAME_PROMPT_VERSION", "v0")

# ダミー応答(APIキーが無い/失敗したときのフォールバック)
_DUMMY_REPLIES = [
    "うーん、考えたことなかったな。なんでそんなこと聞くの?",
    "あー、それね。まあ普通かな。",
    "え、急にどうした(笑)",
    "わからん。てきとうに答えていい?",
    "それ難しい質問だね。んー、たぶんそう思う。",
    "あんまり覚えてないけど、好きだった気がする。",
]

# --- レート制限 (無料枠RPMへの保険) ---
MIN_CALL_INTERVAL_S = 4.5
_rate_lock = threading.Lock()
_last_call_at = 0.0

# クライアントは一度だけ作って使い回す
_client = None


def _get_client():
    global _client
    if _client is None:
        from google import genai
        # APIキーは環境変数 GEMINI_API_KEY から自動で読まれる
        _client = genai.Client()
    return _client


def _wait_for_rate_limit():
    """直前の呼び出しから MIN_CALL_INTERVAL_S 空くまで待つ(プロセス内)。"""
    global _last_call_at
    with _rate_lock:
        now = time.monotonic()
        wait = MIN_CALL_INTERVAL_S - (now - _last_call_at)
        if wait > 0:
            time.sleep(wait)
        _last_call_at = time.monotonic()


def _get_gemini_reply(history, system_prompt):
    """Geminiを使って応答を生成する (新SDK)。"""
    from google.genai import types

    client = _get_client()

    # history: [{"role": "questioner"/"ai", "text": "..."}] を Gemini形式に変換
    contents = []
    for turn in history:
        role = "user" if turn["role"] == "questioner" else "model"
        contents.append(
            types.Content(role=role, parts=[types.Part(text=turn["text"])])
        )

    config = None
    if system_prompt:  # v0(None)のときは config 自体を付けない = 素のGemini
        config = types.GenerateContentConfig(system_instruction=system_prompt)

    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=contents,
        config=config,
    )
    return response.text.strip()


def generate_reply(history, prompt_version=None):
    """
    会話履歴を受け取り、AI(回答者)の次の発言を返す。

    history の形式:
      [{"role": "questioner", "text": "好きな食べ物は?"},
       {"role": "ai",         "text": "ラーメンかな"},
       {"role": "questioner", "text": "なんで?"}]

    prompt_version: PROMPT_VERSIONS のキー。省略時は DEFAULT_PROMPT_VERSION。
    返り値: AIの発言(文字列)
    """
    version = prompt_version or DEFAULT_PROMPT_VERSION
    system_prompt = PROMPT_VERSIONS.get(version)

    if os.environ.get("GEMINI_API_KEY"):
        for attempt in (1, 2):  # 1回だけリトライ
            try:
                _wait_for_rate_limit()
                return _get_gemini_reply(history, system_prompt)
            except Exception as e:
                print(f"[ai_backend] Gemini呼び出し失敗(試行{attempt}): {e}")
                if attempt == 1:
                    time.sleep(2.0)  # 少し置いて再試行(429対策の簡易版)
        print("[ai_backend] リトライも失敗、ダミーに切替")
        return random.choice(_DUMMY_REPLIES)
    else:
        return random.choice(_DUMMY_REPLIES)
