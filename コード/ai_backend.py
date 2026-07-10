"""
AI応答の差し替えポイント。
ここだけ書き換えれば、Gemini / OpenAI / Claude / ローカルLLM のどれにでも切り替えられる。

現在の設定: Gemini無料枠 (新SDK: google-genai)

使い方:
  1. pip install google-genai
  2. https://aistudio.google.com/apikey で無料のAPIキーを取得
  3. 環境変数に設定:  export GEMINI_API_KEY="あなたのキー"
     (Windowsの場合:  set GEMINI_API_KEY=あなたのキー)
  4. app.py を起動

APIキーが無い/呼び出しに失敗した場合は、自動でダミー応答にフォールバックするので、
キー無しでも画面の動作確認だけはできる。
"""

import os
import random

# 使うモデル名。安いモデルに変えたいときはここだけ書き換える。
MODEL_NAME = "gemini-3.5-flash"

# ============================================================
# AIの「人格」を決めるシステムプロンプト。
# ゲームの肝。ここを調整して「AIらしさ」を消したり残したりする。
# ============================================================
SYSTEM_PROMPT = """あなたはオンラインの雑談ゲームに参加している一人の人間プレイヤーです。
相手はあなたが人間かAIかを当てようとしています。

ルール:
- あなたは普通の人間のフリをして、質問に自然に答えてください。
- 答えは短め(1〜3文)で、口語的に。完璧な敬語や説明口調は避ける。
- 「AIとして」とか「私は人間です」とは絶対に言わない。
- 知らないことは適当にごまかしたり、「わからん」と言ってもいい。
- たまに脱線したり、感情(面倒くさい、笑う、など)を出してもいい。
- 長い箇条書きや丁寧すぎる説明はしない。人間はそんな喋り方をしない。
"""

# ダミー応答(APIキーが無い/失敗したときのフォールバック)
_DUMMY_REPLIES = [
    "うーん、考えたことなかったな。なんでそんなこと聞くの?",
    "あー、それね。まあ普通かな。",
    "え、急にどうした(笑)",
    "わからん。てきとうに答えていい?",
    "それ難しい質問だね。んー、たぶんそう思う。",
    "あんまり覚えてないけど、好きだった気がする。",
]

# クライアントは一度だけ作って使い回す
_client = None


def _get_client():
    global _client
    if _client is None:
        from google import genai
        # APIキーは環境変数 GEMINI_API_KEY から自動で読まれる
        _client = genai.Client()
    return _client


def _get_gemini_reply(history):
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

    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
        ),
    )
    return response.text.strip()


def generate_reply(history):
    """
    会話履歴を受け取って、AI(回答者)の次の発言を返す。

    history の形式:
      [{"role": "questioner", "text": "好きな食べ物は?"},
       {"role": "ai",         "text": "ラーメンかな"},
       {"role": "questioner", "text": "なんで?"}]

    返り値: AIの発言(文字列)
    """
    if os.environ.get("GEMINI_API_KEY"):
        try:
            return _get_gemini_reply(history)
        except Exception as e:
            # 失敗してもゲームは止めず、ダミーに落とす
            print(f"[ai_backend] Gemini呼び出し失敗、ダミーに切替: {e}")
            return random.choice(_DUMMY_REPLIES)
    else:
        return random.choice(_DUMMY_REPLIES)
