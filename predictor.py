"""ニュース要約に基づく株価予測ツール(複数モデルの多数決)

collector.py が収集した各キーワードの最新ニュース要約をもとに、
prediction_models.json に列挙した複数のOpenRouter無料LLMモデルそれぞれに
「明日・1週間後・1か月後」の日本株式市場・米国株式市場への影響
(ポジティブ/ネガティブ)を独立に予測させる。

予測の際には、今回の最新ニュース要約に加えて、過去のニュースの流れ(直近1日/
1週間/1か月をさらにLLMで要約したもの。predictions/context/ にMarkdownとして
キャッシュ)と、対応する期間の株価の推移(日経平均・S&P500の日足OHLCVから
算出した騰落率・高値安値レンジ)をコンテキストとしてプロンプトに含める。
明日の予測には直近1日、1週間後の予測には直近1週間、1か月後の予測には
直近1か月のコンテキストをそれぞれ重視させる。

各モデルの予測はモデルごとに別ファイル(predictions/by_model/<モデル名>.json)
に時系列で記録し、モデル間の多数決による最終予測を predictions/log.json に
記録する(同数の場合は "tie" として多数決不成立を明示する)。

また、予測対象日を過ぎた過去の予測については、日経平均(^N225)・S&P500(^GSPC)
の実際の値動きと比較して自動的に答え合わせ(的中/不的中)を行う。答え合わせは
モデルごとのログ・多数決ログの両方に対して独立に行われるため、モデルごとの
予測精度を比較できる。

使い方:
    python predictor.py
    python predictor.py --output-dir output --log-file predictions/log.json

事前準備:
    pip install -r requirements.txt
    .env に OPENROUTER_API_KEY を設定済みであること(collector.pyと共通)
    prediction_models.json で使用するモデルを編集できる
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import yfinance as yf
from dotenv import load_dotenv

from openrouter_client import DailyQuotaExceededError, call_openrouter

REQUEST_INTERVAL_SECONDS = float(os.environ.get("OPENROUTER_REQUEST_INTERVAL", "4"))

DEFAULT_PREDICTION_MODELS = [
    "nex-agi/nex-n2.5-mini:free",
    "liquid/lfm-2.5-2.6b:free",
    "nvidia/nemotron-3.5-lightning:free",
]

MARKET_SYMBOLS = {"japan": "^N225", "us": "^GSPC"}
MARKET_LABELS = {"japan": "日本株(日経平均)", "us": "米国株(S&P500)"}
HORIZON_DAYS = {"tomorrow": 1, "1week": 7, "1month": 30}
HORIZON_LABELS = {"tomorrow": "明日", "1week": "1週間後", "1month": "1か月後"}

CONTEXT_PERIOD_DAYS = {"1day": 1, "1week": 7, "1month": 30}
CONTEXT_PERIOD_LABELS = {"1day": "直近1日", "1week": "直近1週間", "1month": "直近1か月"}
# 株価文脈は暦日ではなく取引日数で切り出す(騰落率算出のため+1日ぶん多めに取得)
PRICE_WINDOW_TRADING_DAYS = {"1day": 2, "1week": 6, "1month": 22}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("predictor")


def slugify_model(model: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", model)


def load_prediction_models(path: Path) -> list[str]:
    if path.exists():
        try:
            models = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(models, list) and models:
                return [str(m) for m in models]
        except Exception as exc:
            logger.warning("prediction_models.json の読み込みに失敗しました: %s", exc)
    return DEFAULT_PREDICTION_MODELS


def load_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("予測ログの読み込みに失敗しました (%s): %s", path, exc)
        return []


def save_log(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


def get_all_reports(output_dir: Path) -> dict[str, list[dict]]:
    """キーワードごとに全レポートを generated_at 昇順で返す(過去の未予測分も対象にするため)。"""
    reports: dict[str, list[dict]] = defaultdict(list)
    for path in sorted(output_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        keyword = data.get("keyword")
        if not keyword:
            continue
        reports[keyword].append(data)
    for keyword_reports in reports.values():
        keyword_reports.sort(key=lambda r: r.get("generated_at", ""))
    return reports


def get_reports_for_keyword_in_window(output_dir: Path, keyword: str, now: datetime, days: int) -> list[dict]:
    """指定キーワードについて、直近days日以内に生成されたレポートを全て返す。"""
    cutoff = now - timedelta(days=days)
    reports = []
    for path in sorted(output_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("keyword") != keyword:
            continue
        gen_at = data.get("generated_at", "")
        try:
            gen_dt = datetime.fromisoformat(gen_at)
        except ValueError:
            continue
        if gen_dt >= cutoff:
            reports.append(data)
    return reports


def build_context_prompt(keyword: str, period_label: str, articles: list[dict]) -> str:
    summaries = "\n".join(f"- {a.get('title', '')}: {a.get('summary', '')}" for a in articles)
    return (
        f"以下は「{keyword}」に関する{period_label}のニュース要約の一覧です(古い順とは限りません)。\n\n"
        f"{summaries}\n\n"
        "この期間全体としてどのような出来事・流れがあったかを把握できるように、日本語3〜5文程度で"
        "簡潔にまとめてください。個々の記事を列挙するのではなく、時系列の変化や全体的な論調を"
        "意識してください。回答は要約文のみとし、前置きや見出し、箇条書き記号は不要です。"
    )


def load_context_cache(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
    if not m:
        return None
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    body = m.group(2).strip()
    if not body:
        return None
    return {"meta": meta, "body": body}


def save_context_md(path: Path, keyword: str, period_label: str, source_generated_at: str, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    front = (
        "---\n"
        f"keyword: {keyword}\n"
        f"period: {period_label}\n"
        f"source_generated_at: {source_generated_at}\n"
        f"updated_at: {datetime.now().isoformat(timespec='seconds')}\n"
        "---\n\n"
    )
    path.write_text(front + body + "\n", encoding="utf-8")


def get_news_context(
    output_dir: Path,
    context_dir: Path,
    keyword: str,
    period: str,
    api_key: str,
    context_model: str,
    now: datetime,
) -> str:
    """過去のニュース要約をさらに要約した文脈テキストを返す(未変化ならキャッシュを再利用)。"""
    reports = get_reports_for_keyword_in_window(output_dir, keyword, now, CONTEXT_PERIOD_DAYS[period])
    articles = [a for r in reports for a in r.get("articles", []) if a.get("summary") and not a.get("error")]
    period_label = CONTEXT_PERIOD_LABELS[period]
    if not articles:
        return f"({period_label}のニュースなし)"

    latest_gen_at = max((r.get("generated_at", "") for r in reports), default="")
    path = context_dir / f"{keyword}_{period}.md"
    cached = load_context_cache(path)
    if cached and cached["meta"].get("source_generated_at") == latest_gen_at:
        return cached["body"]

    try:
        body = call_openrouter(build_context_prompt(keyword, period_label, articles), api_key, context_model).strip()
    except DailyQuotaExceededError:
        raise
    except Exception as exc:
        logger.warning("[%s/%s] ニュース文脈の要約に失敗しました: %s", keyword, period, exc)
        return cached["body"] if cached else f"({period_label}の文脈生成に失敗)"

    save_context_md(path, keyword, period_label, latest_gen_at, body)
    time.sleep(REQUEST_INTERVAL_SECONDS)
    return body


def fetch_price_history(symbol: str, as_of: datetime | None = None):
    """as_of時点までの日足OHLCVを取得する(バックテスト時に未来の値動きが混入しないよう、
    as_ofを指定した場合はその日以降のデータを確実に除外する)。"""
    end_dt = as_of or datetime.now()
    start_str = (end_dt - timedelta(days=95)).strftime("%Y-%m-%d")
    end_str = (end_dt + timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        hist = yf.Ticker(symbol).history(start=start_str, end=end_str, interval="1d")
    except Exception as exc:
        logger.warning("価格履歴取得エラー (%s): %s", symbol, exc)
        return None
    if hist.empty:
        return None
    if hist.index.tz is not None:
        hist.index = hist.index.tz_localize(None)
    hist = hist[hist.index.date <= end_dt.date()]
    if hist.empty:
        return None
    return hist


def summarize_price_window(hist, trading_days: int) -> str:
    if hist is None or hist.empty:
        return "データ取得不可"
    window = hist.tail(trading_days)
    if window.empty:
        return "データ取得不可"

    latest = window.iloc[-1]
    lines = [
        f"直近終値: {float(latest['Close']):,.2f} ({window.index[-1].strftime('%Y-%m-%d')})",
        f"直近取引日の始値/高値/安値/終値: {float(latest['Open']):,.2f} / {float(latest['High']):,.2f} / "
        f"{float(latest['Low']):,.2f} / {float(latest['Close']):,.2f}",
    ]
    if len(window) >= 2:
        start_close = float(window["Close"].iloc[0])
        end_close = float(latest["Close"])
        change_pct = (end_close - start_close) / start_close * 100 if start_close else 0.0
        high = float(window["High"].max())
        low = float(window["Low"].min())
        lines.insert(1, f"期間騰落率: {change_pct:+.2f}% ({window.index[0].strftime('%Y-%m-%d')}終値比)")
        lines.append(f"期間高値/安値: {high:,.2f} / {low:,.2f}")
    return "\n".join(lines)


def build_price_context_cache(price_history_cache: dict[str, object]) -> dict[str, dict[str, str]]:
    return {
        symbol: {
            period: summarize_price_window(hist, trading_days)
            for period, trading_days in PRICE_WINDOW_TRADING_DAYS.items()
        }
        for symbol, hist in price_history_cache.items()
    }


def fetch_price_near(symbol: str, date: datetime, window_days: int = 5) -> tuple[float, str] | None:
    start = (date - timedelta(days=window_days)).strftime("%Y-%m-%d")
    end = (date + timedelta(days=window_days + 1)).strftime("%Y-%m-%d")
    try:
        hist = yf.Ticker(symbol).history(start=start, end=end)
    except Exception as exc:
        logger.warning("価格データ取得エラー (%s): %s", symbol, exc)
        return None
    if hist.empty:
        return None
    if hist.index.tz is not None:
        hist.index = hist.index.tz_localize(None)
    closest = min(hist.index, key=lambda d: abs((d.to_pydatetime().date() - date.date()).days))
    return float(hist.loc[closest, "Close"]), closest.strftime("%Y-%m-%d")


def build_prediction_prompt(
    keyword: str,
    articles: list[dict],
    news_context: dict[str, str],
    price_context: dict[str, dict[str, str]],
) -> str:
    summaries = "\n".join(f"- {a.get('title', '')}: {a.get('summary', '')}" for a in articles)

    news_context_block = "\n\n".join(
        f"【{CONTEXT_PERIOD_LABELS[period]}のニュースの流れ】\n{news_context.get(period, '')}"
        for period in ("1day", "1week", "1month")
    )

    price_context_lines = []
    for market, symbol in MARKET_SYMBOLS.items():
        price_context_lines.append(f"◆{MARKET_LABELS[market]}")
        for period in ("1day", "1week", "1month"):
            text = price_context.get(symbol, {}).get(period, "データ取得不可")
            price_context_lines.append(f"[{CONTEXT_PERIOD_LABELS[period]}]\n{text}")
    price_context_block = "\n".join(price_context_lines)

    return (
        f"以下は「{keyword}」に関する情報です。\n\n"
        f"■ニュースの文脈(過去の要約をさらに要約したもの)\n{news_context_block}\n\n"
        f"■株価の推移(日足の終値・高値・安値ベース)\n{price_context_block}\n\n"
        f"■今回の最新ニュース要約(判定の主材料)\n{summaries}\n\n"
        "この情報をもとに、日本株式市場全体および米国株式市場全体への影響を予測してください。\n"
        "明日の予測には「直近1日」、1週間後の予測には「直近1週間」、1か月後の予測には「直近1か月」の"
        "ニュースの文脈・株価の推移を特に重視し、最新ニュース要約と合わせて総合的に判断してください。\n"
        "明日・1週間後・1か月後のそれぞれについて、株価がポジティブ(上昇方向)かネガティブ"
        "(下落方向)かを判定し、簡潔な理由を日本語で述べてください。\n"
        "必ず次のJSON形式のみで回答し、他の文章は含めないでください。\n\n"
        "{\n"
        '  "japan": {\n'
        '    "tomorrow": {"direction": "positiveまたはnegative", "reason": "理由"},\n'
        '    "1week": {"direction": "...", "reason": "..."},\n'
        '    "1month": {"direction": "...", "reason": "..."}\n'
        "  },\n"
        '  "us": {\n'
        '    "tomorrow": {"direction": "...", "reason": "..."},\n'
        '    "1week": {"direction": "...", "reason": "..."},\n'
        '    "1month": {"direction": "...", "reason": "..."}\n'
        "  }\n"
        "}\n"
    )


def extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
    text = re.sub(r"```$", "", text).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("応答からJSONを抽出できませんでした")
    return json.loads(text[start:end + 1])


def generate_predictions_for_model(
    model: str,
    keyword: str,
    articles: list[dict],
    api_key: str,
    baseline_cache: dict[str, tuple[float, str] | None],
    gen_at: str,
    now: datetime,
    news_context: dict[str, str],
    price_context: dict[str, dict[str, str]],
) -> list[dict]:
    parsed = None
    response = ""
    for parse_attempt in range(1, 3):
        try:
            prompt = build_prediction_prompt(keyword, articles, news_context, price_context)
            response = call_openrouter(prompt, api_key, model)
            parsed = extract_json(response)
            break
        except DailyQuotaExceededError:
            raise
        except Exception as exc:
            logger.warning("[%s/%s] 予測生成エラー(試行%d/2): %s", keyword, model, parse_attempt, exc)
    if parsed is None:
        logger.warning("[%s/%s] 予測生成に失敗したためスキップします。応答: %s", keyword, model, response[:300])
        return []

    records = []
    for market, symbol in MARKET_SYMBOLS.items():
        baseline = baseline_cache.get(symbol)
        for horizon, offset_days in HORIZON_DAYS.items():
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
                "model": model,
                "keyword": keyword,
                "market": market,
                "market_label": MARKET_LABELS[market],
                "horizon": horizon,
                "horizon_label": HORIZON_LABELS[horizon],
                "direction": direction,
                "reason": reason,
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


def generate_all_predictions(
    output_dir: Path,
    context_dir: Path,
    api_key: str,
    models: list[str],
    context_model: str,
    existing_keys_per_model: dict[str, set[tuple[str, str]]],
) -> dict[str, list[dict]]:
    """各キーワードについて、まだ予測していない全レポート(過去の未予測分も含む)を対象に予測を生成する。

    レポートごとに、そのレポートの生成時刻(gen_at)を「予測時点」として扱う。これにより
    過去のレポートに対しては、その時点までの株価データのみを用いてバックテスト的に予測を
    生成でき、未来の値動きが文脈に混入する(リークする)ことを防ぐ。
    """
    all_reports = get_all_reports(output_dir)

    baseline_cache_by_date: dict[str, dict[str, tuple[float, str] | None]] = {}
    price_context_by_date: dict[str, dict[str, dict[str, str]]] = {}

    def baseline_cache_for(report_now: datetime, date_key: str) -> dict[str, tuple[float, str] | None]:
        if date_key not in baseline_cache_by_date:
            baseline_cache_by_date[date_key] = {
                symbol: fetch_price_near(symbol, report_now) for symbol in MARKET_SYMBOLS.values()
            }
        return baseline_cache_by_date[date_key]

    def price_context_for(report_now: datetime, date_key: str) -> dict[str, dict[str, str]]:
        if date_key not in price_context_by_date:
            price_history_cache = {
                symbol: fetch_price_history(symbol, as_of=report_now) for symbol in MARKET_SYMBOLS.values()
            }
            price_context_by_date[date_key] = build_price_context_cache(price_history_cache)
        return price_context_by_date[date_key]

    results: dict[str, list[dict]] = {model: [] for model in models}

    for keyword, keyword_reports in all_reports.items():
        for report in keyword_reports:
            gen_at = report.get("generated_at", "")
            articles = [a for a in report.get("articles", []) if a.get("summary") and not a.get("error")]
            if not articles:
                continue

            needs_prediction = any(
                (keyword, gen_at) not in existing_keys_per_model.get(model, set()) for model in models
            )
            if not needs_prediction:
                continue

            try:
                report_now = datetime.fromisoformat(gen_at)
            except ValueError:
                report_now = datetime.now()
            date_key = report_now.strftime("%Y-%m-%d")

            baseline_cache = baseline_cache_for(report_now, date_key)
            price_context = price_context_for(report_now, date_key)

            try:
                news_context = {
                    period: get_news_context(output_dir, context_dir, keyword, period, api_key, context_model, report_now)
                    for period in CONTEXT_PERIOD_DAYS
                }
            except DailyQuotaExceededError as exc:
                logger.error("%s", exc)
                logger.error("1日の無料リクエスト上限に達したため、今回はここで生成を打ち切ります。")
                return results

            for model in models:
                if (keyword, gen_at) in existing_keys_per_model.get(model, set()):
                    continue
                logger.info("[%s / %s] %s で株価予測を生成中...", keyword, gen_at, model)
                try:
                    records = generate_predictions_for_model(
                        model, keyword, articles, api_key, baseline_cache, gen_at, report_now, news_context, price_context
                    )
                except DailyQuotaExceededError as exc:
                    logger.error("%s", exc)
                    logger.error("1日の無料リクエスト上限に達したため、今回はここで生成を打ち切ります。")
                    return results
                results[model].extend(records)
                time.sleep(REQUEST_INTERVAL_SECONDS)

    return results


def build_consensus_records(results_per_model: dict[str, list[dict]]) -> list[dict]:
    groups: dict[tuple[str, str, str, str], list[dict]] = defaultdict(list)
    for model, records in results_per_model.items():
        for rec in records:
            key = (rec["keyword"], rec["source_generated_at"], rec["market"], rec["horizon"])
            groups[key].append(rec)

    consensus: list[dict] = []
    for (keyword, gen_at, market, horizon), recs in groups.items():
        positive = sum(1 for r in recs if r["direction"] == "positive")
        negative = sum(1 for r in recs if r["direction"] == "negative")
        if positive > negative:
            direction = "positive"
        elif negative > positive:
            direction = "negative"
        else:
            direction = "tie"

        sample = recs[0]
        consensus.append({
            "keyword": keyword,
            "market": market,
            "market_label": sample["market_label"],
            "horizon": horizon,
            "horizon_label": sample["horizon_label"],
            "direction": direction,
            "votes": {"positive": positive, "negative": negative, "total": len(recs)},
            "reasons": {r["model"]: r["reason"] for r in recs},
            "predicted_at": max(r["predicted_at"] for r in recs),
            "source_generated_at": gen_at,
            "target_date": sample["target_date"],
            "baseline_symbol": sample["baseline_symbol"],
            "baseline_price": sample["baseline_price"],
            "baseline_date": sample["baseline_date"],
            "outcome": "tie" if direction == "tie" else "",
            "actual_price": None,
            "actual_date": "",
            "actual_change_pct": None,
            "evaluated_at": "",
        })

    return consensus


def evaluate_due_predictions(records: list[dict]) -> int:
    today = datetime.now().date()
    updated = 0

    for rec in records:
        if rec.get("outcome"):
            continue
        try:
            target_date = datetime.strptime(rec["target_date"], "%Y-%m-%d").date()
        except (KeyError, ValueError):
            continue
        if target_date > today:
            continue

        now_str = datetime.now().isoformat(timespec="seconds")

        if rec.get("baseline_price") is None:
            rec["outcome"] = "no_data"
            rec["evaluated_at"] = now_str
            updated += 1
            continue

        result = fetch_price_near(rec["baseline_symbol"], datetime.combine(target_date, datetime.min.time()))
        if result is None:
            rec["outcome"] = "no_data"
            rec["evaluated_at"] = now_str
            updated += 1
            continue

        actual_price, actual_date = result

        if actual_date == rec.get("baseline_date"):
            # 対象日がまだ取引されていない(週末・休場等)ため、基準日と同じ終値しか
            # 取得できていない。実際の値動きがまだ無いので判定を保留し、後日再評価する。
            # ただし取引日が現れないまま長期間経過した場合はデータなし扱いにする。
            if (today - target_date).days >= 10:
                rec["outcome"] = "no_data"
                rec["evaluated_at"] = now_str
                updated += 1
            continue

        change_pct = (actual_price - rec["baseline_price"]) / rec["baseline_price"] * 100
        actual_direction = "positive" if change_pct > 0 else "negative"

        rec["actual_price"] = actual_price
        rec["actual_date"] = actual_date
        rec["actual_change_pct"] = round(change_pct, 2)
        rec["outcome"] = "hit" if actual_direction == rec["direction"] else "miss"
        rec["evaluated_at"] = now_str
        updated += 1

    return updated


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="複数モデルの多数決による株価予測と的中ログ管理")
    base = Path(__file__).parent
    parser.add_argument("--output-dir", type=str, default=str(base / "output"))
    parser.add_argument("--log-file", type=str, default=str(base / "predictions" / "log.json"),
                         help="多数決による最終予測ログ")
    parser.add_argument("--by-model-dir", type=str, default=str(base / "predictions" / "by_model"),
                         help="モデルごとの個別予測ログを保存するフォルダ")
    parser.add_argument("--models-file", type=str, default=str(base / "prediction_models.json"))
    parser.add_argument("--context-dir", type=str, default=str(base / "predictions" / "context"),
                         help="ニュース文脈(過去要約のさらなる要約)のMarkdownキャッシュを保存するフォルダ")
    return parser.parse_args()


def main() -> int:
    load_dotenv()
    args = parse_args()

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        logger.error(
            "OPENROUTER_API_KEY が設定されていません。"
            ".env.example を .env にコピーしてAPIキーを設定してください。"
        )
        return 1

    output_dir = Path(args.output_dir)
    consensus_log_path = Path(args.log_file)
    by_model_dir = Path(args.by_model_dir)
    context_dir = Path(args.context_dir)
    models = load_prediction_models(Path(args.models_file))
    context_model = models[0]

    logger.info("使用モデル(%d件): %s", len(models), ", ".join(models))
    logger.info("ニュース文脈の要約には %s を使用します。", context_model)

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

    existing_keys_per_model = {
        model: {(r["keyword"], r["source_generated_at"]) for r in model_logs[model]}
        for model in models
    }
    new_per_model = generate_all_predictions(
        output_dir, context_dir, api_key, models, context_model, existing_keys_per_model
    )

    for model in models:
        model_logs[model].extend(new_per_model.get(model, []))
        save_log(by_model_dir / f"{slugify_model(model)}.json", model_logs[model])

    new_consensus = build_consensus_records(new_per_model)
    consensus_records.extend(new_consensus)
    save_log(consensus_log_path, consensus_records)

    print()
    print(f"多数決ログ: {consensus_log_path} (新規{len(new_consensus)}件 / 合計{len(consensus_records)}件)")
    for model in models:
        new_count = len(new_per_model.get(model, []))
        total_count = len(model_logs[model])
        hits = sum(1 for r in model_logs[model] if r.get("outcome") == "hit")
        misses = sum(1 for r in model_logs[model] if r.get("outcome") == "miss")
        evaluated_total = hits + misses
        rate_str = f"的中率 {hits}/{evaluated_total} ({hits / evaluated_total * 100:.1f}%)" if evaluated_total else "判定待ちのみ"
        print(f"  [{model}] 新規{new_count}件 / 合計{total_count}件 / {rate_str}")

    consensus_hits = sum(1 for r in consensus_records if r.get("outcome") == "hit")
    consensus_misses = sum(1 for r in consensus_records if r.get("outcome") == "miss")
    consensus_evaluated_total = consensus_hits + consensus_misses
    if consensus_evaluated_total:
        print(f"多数決の的中率: {consensus_hits}/{consensus_evaluated_total} ({consensus_hits / consensus_evaluated_total * 100:.1f}%)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
