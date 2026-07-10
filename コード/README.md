# 人工知能判別ゲーム ― プロトタイプ

非対称型1対1 × 役割質問式。あなたが質問者となり、相手(AI)に質問して人間かAIかを当てる。
ゲーム終了時に構成・会話ログ・判定結果がデータベースに保存され、判定後に任意でアンケートに答えられる。

## ファイル構成
- `app.py`         … サーバー本体 (Flask)。DB保存・アンケートも担当
- `ai_backend.py`  … AI応答の差し替えポイント (現在: Gemini無料枠)
- `database.py`    … ゲーム記録のDB (SQLite)。保存と書き出し
- `templates/index.html` … プレイ画面＋アンケート画面

## 動かし方

### 1. インストール
```
pip install flask google-generativeai
```

### 2. Geminiの無料APIキーを設定 (任意)
https://aistudio.google.com/apikey でキーを取得し、環境変数に設定:
```
export GEMINI_API_KEY="あなたのキー"      # Mac/Linux
set GEMINI_API_KEY=あなたのキー            # Windows
```
※ キー無しでもダミー応答モードで動作確認できる。

### 3. 起動
```
python app.py
```
ブラウザで http://127.0.0.1:5000 を開く。

## データベース

ゲームを遊ぶと `game_data.db` (SQLite) に自動保存される。4テーブル構成:
- `games`    … ゲーム単位 (入り口の種類、人数、AIの有無/人数、モデル、勝敗)
- `players`  … プレイヤー単位 (役割、投票先、正誤)
- `messages` … 会話ログ (質問と返し)
- `surveys`  … アンケート (確信度、根拠、自由記述) ※任意

すべて `game_id` で繋がっているので、後から自由に分析できる。

### データの取り出し
```
python -c "import database as db; db.export_json()"          # export.json に全部
python -c "import database as db; db.export_csv('messages')" # messages.csv
python -c "import database as db; db.export_csv('games')"    # games.csv
```

## カスタマイズの勘所
- AIの「人格」: `ai_backend.py` の `SYSTEM_PROMPT` を編集
- 質問回数: `app.py` の `MAX_TURNS` を変更
- 別のモデルに変える: `ai_backend.py` の `generate_reply()` だけ書き換え
- 「相手が人間」モードを足す: `app.py` の `/start` で `session["opponent"]` を
  `random.choice(["ai", "human"])` に (人間役の接続は別途必要)
- アンケート項目: `templates/index.html` の survey 部分と `app.py` の `/survey` を対応させる

## まだ実装していないこと (今後)
- オンライン対戦 (最大10人、リアルタイム通信、3つの入り口、AIランダム)
- 大人数型の役割質問式の進行管理
- データの匿名化処理 (研究公開時)
