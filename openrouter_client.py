"""OpenRouter Chat Completions APIを呼び出す共通クライアント

collector.py と predictor.py の両方から利用される。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import requests

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MAX_RETRIES = 3

logger = logging.getLogger("openrouter_client")


class DailyQuotaExceededError(RuntimeError):
    """OpenRouter無料モデルの1日あたりのリクエスト上限に達した場合に送出する。
    時間を置いても当日中は回復しないため、呼び出し側は即座に処理を打ち切るべき。
    """


def call_openrouter(prompt: str, api_key: str, model: str, temperature: float = 0.3) -> str:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://local.news-summarizer",
        "X-Title": "News Summarizer",
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=60)
        except requests.RequestException as exc:
            logger.warning("OpenRouter接続エラー (試行%d/%d): %s", attempt, MAX_RETRIES, exc)
            time.sleep(2 * attempt)
            continue

        if resp.status_code == 200:
            data = resp.json()
            content = data["choices"][0]["message"].get("content")
            if not content:
                logger.warning("OpenRouterから空の応答 (試行%d/%d): %s", attempt, MAX_RETRIES, str(data)[:300])
                time.sleep(2 * attempt)
                continue
            return content.strip()

        if resp.status_code == 429:
            if "free-models-per-day" in resp.text or "per-day" in resp.text:
                reset_str = ""
                reset_ms = resp.headers.get("X-RateLimit-Reset")
                if reset_ms:
                    try:
                        reset_dt = datetime.fromtimestamp(int(reset_ms) / 1000, tz=timezone.utc)
                        reset_str = f"(リセット予定: {reset_dt.isoformat()})"
                    except (ValueError, OSError):
                        pass
                raise DailyQuotaExceededError(
                    f"OpenRouter無料モデルの1日あたりのリクエスト上限に達しました{reset_str}。"
                    "時間を置くか、OpenRouterでクレジットを追加して上限を引き上げてください。"
                )

            retry_after = float(resp.headers.get("Retry-After", 5 * attempt))
            logger.warning("レート制限 (試行%d/%d): %s秒待機", attempt, MAX_RETRIES, retry_after)
            time.sleep(retry_after)
            continue

        raise RuntimeError(f"OpenRouter APIエラー: {resp.status_code} {resp.text[:300]}")

    raise RuntimeError("OpenRouter APIへのリクエストが上限回数失敗しました")
