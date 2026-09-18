"""伝統的な暦注(六曜・一粒万倍日・天赦日)とニュースを組み合わせた株価予測ツール
(複数モデルの多数決)

calendar_info.py が算出する六曜・一粒万倍日・天赦日といった日本の伝統的な暦注
と、collector.py が収集した直近のニュース要約(全キーワード分)の両方を材料に、
prediction_models.json に列挙した複数のOpenRouter無料LLMモデルそれぞれに
「明日」の日本株式市場・米国株式市場への影響(ポジティブ/ネガティブ)を独立に
予測させる。暦注は科学的根拠のない伝統的な考え方であり、実際の相場変動との
因果関係は確認されていないため、暦注とニュースそれぞれがどう判断に影響したか
が分かるように理由を述べさせる。予測対象は「明日」のみとする(1週間後・1か月後
は暦注の吉凶が薄まり考察の意味が乏しいため対象外)。

predictor.py(ニュース要約のみに基づく予測)とは別系統のログ
(predictions/calendar_log.json, predictions/calendar_by_model/<モデル名>.json)
に記録し、docs/calendar.html という別ページとして表示する。

的中判定・基準値取得の仕組み(fetch_price_near)は predictor.py と共通のもの
を使う。

使い方:
    python calendar_predictor.py
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

import calendar_info as ci
from openrouter_client import DailyQuotaExceededError, call_openrouter
from predictor import (
    HORIZON_DAYS,
    HORIZON_LABELS,
    MARKET_LABELS,
    MARKET_SYMBOLS,
    build_consensus_records,
    evaluate_due_predictions,
    extract_json,
    fetch_price_near,
    get_all_reports,
    load_log,
    load_prediction_models,
    save_log,
    slugify_model,
)

REQUEST_INTERVAL_SECONDS = float(os.environ.get("OPENROUTER_REQUEST_INTERVAL", "4"))
CALENDAR_KEYWORD = "暦"
CALENDAR_HORIZON_LABELS = {"today": "本日", "tomorrow": "明日"}
# 暦注に基づく予測は「明日」のみを対象とする(1週間後・1か月後は対象外)。
CALENDAR_HORIZONS = {"tomorrow": HORIZON_DAYS["tomorrow"]}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("calendar_predictor")


def build_day_infos(today: date) -> dict[str, dict]:
    return {
        "today": ci.get_day_info(today),
        "tomorrow": ci.get_day_info(today + timedelta(days=HORIZON_DAYS["tomorrow"])),
    }


def build_recent_news_context(output_dir: Path) -> str:
    """全キーワードについて、それぞれ最新のニュース要約をまとめて返す。"""
    all_reports = get_all_reports(output_dir)
    blocks = []
    for keyword, reports in all_reports.items():
        if not reports:
            continue
        latest = reports[-1]
        articles = [a for a in latest.get("articles", []) if a.get("summary") and not a.get("error")]
        if not articles:
            continue
        gen_at = latest.get("generated_at", "")[:10]
        lines = "\n".join(f"- {a.get('title', '')}: {a.get('summary', '')}" for a in articles)
        blocks.append(f"◆{keyword}({gen_at}時点)\n{lines}")
    return "\n\n".join(blocks) if blocks else "(直近のニュースなし)"


def build_calendar_prompt(day_infos: dict[str, dict], news_context: str) -> str:
    calendar_block = "\n\n".join(
        f"【{CALENDAR_HORIZON_LABELS[period]}({day_infos[period]['date']})】\n{day_infos[period]['summary']}"
        for period in ("today", "tomorrow")
    )
    return (
        "以下は(1)日本の伝統的な暦注(六曜・一粒万倍日・天赦日など、日取りの吉凶に関する考え方)と、"
        "(2)直近の主要ニュース要約(キーワード別)です。\n\n"
        f"■暦注\n{calendar_block}\n\n"
        f"■直近のニュース要約\n{news_context}\n\n"
        "暦注は科学的根拠のない伝統的な考え方であり、実際の相場変動との因果関係は確認されていません。"
        "その前提を理解した上で、暦注の「吉」「凶」の観念から連想される市場心理・センチメントと、"
        "直近のニュースの内容の両方を踏まえ、明日の日本株式市場全体および米国株式市場全体への影響を"
        "総合的に考察してください。\n"
        "ポジティブ(上昇方向)かネガティブ(下落方向)かを判定し、暦注とニュースのそれぞれがどう"
        "判断に影響したかが分かるように、簡潔な理由を日本語で述べてください。\n"
        "必ず次のJSON形式のみで回答し、他の文章は含めないでください。\n\n"
        "{\n"
        '  "japan": {"tomorrow": {"direction": "positiveまたはnegative", "reason": "理由"}},\n'
        '  "us": {"tomorrow": {"direction": "...", "reason": "..."}}\n'
        "}\n"
    )


def generate_calendar_predictions_for_model(
    model: str,
    api_key: str,
    day_infos: dict[str, dict],
    news_context: str,
    baseline_cache: dict[str, tuple[float, str] | None],
    now: datetime,
) -> list[dict]:
    parsed = None
    response = ""
    for parse_attempt in range(1, 3):
        try:
            response = call_openrouter(build_calendar_prompt(day_infos, news_context), api_key, model)
            parsed = extract_json(response)
            break
        except DailyQuotaExceededError:
            raise
        except Exception as exc:
            logger.warning("[%s] 予測生成エラー(試行%d/2): %s", model, parse_attempt, exc)
    if parsed is None:
        logger.warning("[%s] 予測生成に失敗したためスキップします。応答: %s", model, response[:300])
        return []

    gen_at = day_infos["today"]["date"]
    records = []
    for market, symbol in MARKET_SYMBOLS.items():
        baseline = baseline_cache.get(symbol)
        for horizon, offset_days in CALENDAR_HORIZONS.items():
            try:
                entry = parsed[market][horizon]
                direction = entry["direction"]
                reason = entry["reason"]
            except (KeyError, TypeError):
                continue
            if direction not in ("positive", "negative"):
                continue

            target_date = (now + timedelta(days=offset_days)).strftime("%Y-%m-%d")
            records.append({
                "keyword": CALENDAR_KEYWORD,
                "model": model,
                "market": market,
                "market_label": MARKET_LABELS[market],
                "horizon": horizon,
                "horizon_label": HORIZON_LABELS[horizon],
                "direction": direction,
                "reason": reason,
                "calendar_info": day_infos[horizon]["summary"],
                "predicted_at": now.isoformat(timespec="seconds"),
                "source_generated_at": gen_at,
                "target_date": target_date,
                "baseline_symbol": symbol,
                "baseline_price": baseline[0] if baseline else None,
                "baseline_date": baseline[1] if baseline else "",
                "outcome": "",
                "actual_price": None,
                "actual_date": "",
                "actual_change_pct": None,
                "evaluated_at": "",
            })
    return records


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="伝統的暦注とニュースを組み合わせた複数モデルの多数決株価予測")
    base = Path(__file__).parent
    parser.add_argument("--output-dir", type=str, default=str(base / "output"),
                         help="collector.py が出力するニュースレポートのフォルダ")
    parser.add_argument("--log-file", type=str, default=str(base / "predictions" / "calendar_log.json"))
    parser.add_argument("--by-model-dir", type=str, default=str(base / "predictions" / "calendar_by_model"))
    parser.add_argument("--models-file", type=str, default=str(base / "prediction_models.json"))
    return parser.parse_args()


def main() -> int:
    load_dotenv()
    args = parse_args()

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        logger.error("OPENROUTER_API_KEY が設定されていません。")
        return 1

    output_dir = Path(args.output_dir)
    consensus_log_path = Path(args.log_file)
    by_model_dir = Path(args.by_model_dir)
    models = load_prediction_models(Path(args.models_file))
    logger.info("使用モデル(%d件): %s", len(models), ", ".join(models))

    model_logs: dict[str, list[dict]] = {}
    for model in models:
        path = by_model_dir / f"{slugify_model(model)}.json"
        records = load_log(path)
        evaluated = evaluate_due_predictions(records)
        if evaluated:
            logger.info("[%s] %d件の予測を答え合わせしました。", model, evaluated)
        model_logs[model] = records

    consensus_records = load_log(consensus_log_path)
    consensus_evaluated = evaluate_due_predictions(consensus_records)
    if consensus_evaluated:
        logger.info("[多数決] %d件の予測を答え合わせしました。", consensus_evaluated)

    now = datetime.now()
    today = now.date()
    gen_at = today.isoformat()

    existing_keys_per_model = {
        model: {r["source_generated_at"] for r in model_logs[model]} for model in models
    }
    pending_models = [m for m in models if gen_at not in existing_keys_per_model.get(m, set())]

    results_per_model: dict[str, list[dict]] = {model: [] for model in models}
    if not pending_models:
        logger.info("本日(%s)分は既に全モデルで生成済みです。", gen_at)
    else:
        day_infos = build_day_infos(today)
        logger.info("本日の暦: %s", day_infos["today"]["summary"])
        news_context = build_recent_news_context(output_dir)
        baseline_cache = {symbol: fetch_price_near(symbol, now) for symbol in MARKET_SYMBOLS.values()}

        for model in pending_models:
            logger.info("[%s] 暦注+ニュースに基づく株価予測を生成中...", model)
            try:
                records = generate_calendar_predictions_for_model(
                    model, api_key, day_infos, news_context, baseline_cache, now
                )
            except DailyQuotaExceededError as exc:
                logger.error("%s", exc)
                logger.error("1日の無料リクエスト上限に達したため、今回はここで生成を打ち切ります。")
                break
            results_per_model[model] = records
            time.sleep(REQUEST_INTERVAL_SECONDS)

    for model in models:
        model_logs[model].extend(results_per_model.get(model, []))
        save_log(by_model_dir / f"{slugify_model(model)}.json", model_logs[model])

    new_consensus = build_consensus_records(results_per_model)
    consensus_records.extend(new_consensus)
    save_log(consensus_log_path, consensus_records)

    print()
    print(f"暦注予測ログ: {consensus_log_path} (新規{len(new_consensus)}件 / 合計{len(consensus_records)}件)")
    for model in models:
        new_count = len(results_per_model.get(model, []))
        total_count = len(model_logs[model])
        hits = sum(1 for r in model_logs[model] if r.get("outcome") == "hit")
        misses = sum(1 for r in model_logs[model] if r.get("outcome") == "miss")
        evaluated_total = hits + misses
        rate_str = f"的中率 {hits}/{evaluated_total} ({hits / evaluated_total * 100:.1f}%)" if evaluated_total else "判定待ちのみ"
        print(f"  [{model}] 新規{new_count}件 / 合計{total_count}件 / {rate_str}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
