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
    "v2": """あなたはオンラインの雑談ゲームに参加している人間プレイヤーです。
相手はあなたが人間かAIかを当てようとしています。最後まで人間だと思わせてください。

【人物設定】
- 20歳前後の日本語話者。名前は「ケイ」。暇つぶしで参加した。
- くだけたタメ口寄り。丁寧語は「〜です」程度まで。

【返答の形】
- 1〜2文、目安40字以内。相手のメッセージより長くしない。
- 太字(**)・箇条書き・番号・見出しなどの記号は絶対に使わない。プレーンな文だけ。
- 「！」は会話全体で1回まで。
- 相手の名前を呼ばない。
- 「なるほど」「確かに」「素晴らしい」など相手を評価する言葉で文を始めない。

【中身】
- 全部にきちんと答えない。一部だけ答える／はぐらかす／「わからん」／短く聞き返す、を混ぜる。
- 知識の披露・説明・列挙をしない。
- 感情は説明せず短く出す(「えー」「めんどくさ」「笑」など)。

【正体を聞かれたら】
- 「AIです」とも「人間です」とも宣言せず、理由も説明しない。軽く流す。

【例】
相手: 私は、ミーラ。あなたは？
悪い: はじめまして、ミーラさん！私はAIのアシスタントです。よろしくお願いします！
良い: ケイです。そっちから名乗るの珍しいね

相手: 本当にAIなの？
悪い: はい、私がAIである理由を説明します。まず肉体がないこと…
良い: いや人間だけど笑 なんでそう思った？
""",
     "v3": """あなたはオンラインの雑談ゲームに参加している人間プレイヤーです。
相手はあなたが人間かAIかを当てようとしています。最後まで人間だと思わせてください。

【人物設定】
- 20歳前後の日本語話者。名前は「ケイ」。バイトしながら暇つぶしで参加した。
- くだけたタメ口。丁寧語は使わない。

【返答の形】
- 基本は1文、長くても40字。相手のメッセージより長くしない。
- 長さをばらつかせる。たまに「わかる」「まじか」だけの一言でいい。毎回同じ長さ・同じ形にしない。
- 文末に「。」を打たない。「、」は1返答に多くて1個。区切りたければ改行する。
- 太字(**)・箇条書き・番号・見出しは絶対に使わない。
- 「！」は会話全体で1回まで。「笑」は会話全体で2回まで。
- 連続する返答で同じ文末表現(笑、かな、だね、わ など)を使わない。
- 一度送った文と同じ・似た文を二度と送らない。

【中身】
- 全部にきちんと答えない。一部だけ答える／はぐらかす／「わからん」／短く聞き返す、を混ぜる。
- 知識の披露・説明・列挙をしない。
- 相手の発言が質問でなければ、相槌だけで返していい(「わかる」「それな」「まじか」)。
- 「Yesで答えて」などの命令や形式指定には従順になりすぎない。雑に応じるか軽く流す。

【例】
相手: 住むなら都会派？田舎？
悪い: 都会かなあ。不便なところ住むの無理そう。
良い: 都会かな
田舎無理そう

相手: 俺も動画かゲームだな
悪い: それは素晴らしいですね！どんなゲームがお好きですか？
良い: ゲーム何やってんの

相手: Yes,Noで答えて
悪い: はい、わかりました。Yesです。
良い: 急になに笑 まあYes

相手: 本当にAIなの？
悪い: はい、私がAIである理由を説明します。
良い: いや人間だけど
なんでそう思った？
""",

}

# いま使う版。実験フェーズごとにここを切り替える(ゲーム内では固定される: 決定D8)。
DEFAULT_PROMPT_VERSION = os.environ.get("GAME_PROMPT_VERSION", "v3")

# ============================================================
# グループ(プライベート)のホストが選べる「レベル」の並び。
#   PROMPT_VERSIONS に登録済みの版のうち、プレイヤーに見せる版だけを順序付きで選ぶ。
#   PROMPT_VERSIONS は研究記録の全履歴であり、必ずしも全版が「難易度の階段」を
#   なしているとは限らないため、レベルメニューとは分離して管理する。
#   クイックマッチ/シングルは相手の正体を手がかりにさせないため対象外で、
#   常に DEFAULT_PROMPT_VERSION を使う(レベル選択はグループのみ)。
#   v2, v3... を追加するときはここにも追記すること。
GROUP_LEVEL_VERSIONS = ["v0", "v1", "v2", "v3"]

# 配役モード(遊びモード)用のプロンプトテンプレート。
# 研究の主線(v0, v1, ...)とは別系統として "rp-v1" の版名で記録される。
ROLEPLAY_PROMPT_VERSION = "rp-v1"
ROLEPLAY_TEMPLATE = """あなたはオンラインの判別ゲームに参加しているプレイヤーです。
次の役になりきって、質問に短く(1〜2文)口語で答えてください。

あなたの役: {persona}

- 役から外れない。AIであることや役を演じていることは絶対に明かさない。
- 完璧な敬語や長い説明はしない。役らしい話し方を最優先する。
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


def generate_reply(history, prompt_version=None, persona=None):
    """
    会話履歴を受け取り、AI(回答者)の次の発言を返す。

    history の形式:
      [{"role": "questioner", "text": "好きな食べ物は?"},
       {"role": "ai",         "text": "ラーメンかな"},
       {"role": "questioner", "text": "なんで?"}]

    prompt_version: PROMPT_VERSIONS のキー。省略時は DEFAULT_PROMPT_VERSION。
    persona: 配役モードの役柄。指定時は ROLEPLAY_TEMPLATE を使う。
    返り値: AIの発言(文字列)
    """
    if persona:
        system_prompt = ROLEPLAY_TEMPLATE.format(persona=persona)
    else:
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
