# -*- coding: utf-8 -*-
"""
delay_model.py ― b1(タイミング中立化)の実装。決定メモ§2・§3に対応。

2つの役割を持つ:
  1. AI応答遅延の生成      … 遅延 = 思考時間 + 文字数比例 + ノイズ (決定D5/D8)
  2. AI切替デッドラインWのドロー … クイックマッチのW事前ドロー方式 (決定D4)

【重要】数値はすべて暫定のプレースホルダ (決定メモ§7)。
プロトタイプで計測する messages.compose_time_ms / char_count の実測分布で
置き換える前提。更新したら必ず DELAY_MODEL_VERSION を上げること
(games.delay_model_version に記録され、解析で世代を区別できる)。

環境変数 GAME_FAST_DELAY=1 でテスト用に遅延ほぼゼロになる
(自動テストで数十秒待たないため。研究データ収集時は絶対に設定しないこと)。
"""

import os
import random

# ============================================================
# バージョン識別子。パラメータを変えたら必ず更新する。
# 命名例: d0-placeholder → d1-proto-calibrated → d2-...
# ============================================================
DELAY_MODEL_VERSION = "d0-placeholder"

# --- AI応答遅延のパラメータ (暫定) ---
PARAMS = {
    "thinking_min_s": 3.0,   # 思考時間の下限 (読み終わって打ち始めるまで)
    "thinking_max_s": 8.0,   # 思考時間の上限
    "cps_min": 1.5,          # 打鍵速度の下限 (文字/秒) ※日本語想定の当て推量
    "cps_max": 2.5,          # 打鍵速度の上限
    "noise_sd_s": 0.8,       # 正規ノイズの標準偏差
    "delay_min_s": 1.0,      # 遅延の下限 (瞬間応答を防ぐ)
    "delay_max_s": 40.0,     # 遅延の上限 (長文でも待たせすぎない)
}

# --- W(AI切替デッドライン)の分布パラメータ (暫定: 一様20〜45秒) ---
# 公開後、「人間マッチが成立するまでの実測待ち時間」の分布で較正して置き換える。
W_PARAMS = {
    "w_min_s": 20.0,
    "w_max_s": 45.0,
}


def _fast_mode():
    return os.environ.get("GAME_FAST_DELAY") == "1"


def draw_reply_delay_s(char_count: int) -> float:
    """
    AIの返答1件ぶんの表示遅延(秒)を引く。

    形: 思考時間(一様) + 文字数/打鍵速度(一様) + 正規ノイズ を
    [delay_min_s, delay_max_s] にクリップ。
    引いた値は必ず messages.displayed_delay_ms に記録すること。
    """
    if _fast_mode():
        return 0.01
    p = PARAMS
    thinking = random.uniform(p["thinking_min_s"], p["thinking_max_s"])
    typing = char_count / random.uniform(p["cps_min"], p["cps_max"])
    noise = random.gauss(0.0, p["noise_sd_s"])
    delay = thinking + typing + noise
    return max(p["delay_min_s"], min(p["delay_max_s"], delay))


def draw_ai_switch_deadline_s() -> float:
    """
    クイックマッチ用: キュー投入時に1回だけ引くAI切替デッドラインW(秒)。
    W到達までは人間マッチを探索し、未マッチならAI戦へ切り替える。
    引いた値は必ず games.ai_switch_deadline_ms に記録すること。
    """
    if _fast_mode():
        return 0.5
    return random.uniform(W_PARAMS["w_min_s"], W_PARAMS["w_max_s"])


def params_snapshot() -> dict:
    """記録用: 現在のパラメータ一式(解析時の再現に使える)。"""
    return {
        "version": DELAY_MODEL_VERSION,
        "reply_delay": dict(PARAMS),
        "w": dict(W_PARAMS),
    }
