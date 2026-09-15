"""ニュース自動収集・要約ツール

keywords.txt のキーワードごとに、sources.json で定義した複数のRSSソースから
関連ニュースを収集し、OpenRouterの無料LLMモデルで要約する。
結果は「タイトル・URL・要約」の3項目でJSON/Markdownに保存する。
詳細な設計は design_document.md を参照。

使い方:
    python collector.py
    python collector.py --keywords-file keywords.txt --sources-file sources.json

事前準備:
    pip install -r requirements.txt
    .env.example を .env にコピーし、OPENROUTER_API_KEY を設定する
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import feedparser
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from openrouter_client import call_openrouter

DEFAULT_MODEL = "google/gemini-2.5-flash:free"
REQUEST_INTERVAL_SECONDS = float(os.environ.get("OPENROUTER_REQUEST_INTERVAL", "4"))
FETCH_INTERVAL_SECONDS = 1.0
MAX_RESULTS_PER_SOURCE = 5
MAX_ARTICLES_PER_KEYWORD = 8
ARTICLE_FETCH_TIMEOUT = 10
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) news-summarizer/1.0"
DISCLAIMER = "本レポートは自動生成された参考情報です。内容の正確性は保証されません。投資助言ではありません。"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("news_summarizer")


@dataclass
class NewsItem:
    keyword: str
    source: str
    title: str
    url: str
    published: str = ""
    snippet: str = ""
    body_excerpt: str = ""
    summary: str = ""
    collected_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    error: str = ""


def load_keywords(path: Path) -> list[str]:
    keywords = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        keywords.append(line)
    return keywords


def load_sources(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


def strip_html(html_text: str) -> str:
    if not html_text:
        return ""
    return BeautifulSoup(html_text, "html.parser").get_text(separator=" ", strip=True)


def fetch_query_source(source: dict, keyword: str, max_results: int) -> list[NewsItem]:
    try:
        query = urllib.parse.quote(keyword)
        url = source["url_template"].format(query=query)
        feed = feedparser.parse(url)
        items = []
        for entry in feed.entries[:max_results]:
            items.append(NewsItem(
                keyword=keyword,
                source=source["name"],
                title=entry.get("title", ""),
                url=entry.get("link", ""),
                published=entry.get("published", ""),
                snippet=strip_html(entry.get("summary", "")),
            ))
        return items
    except Exception as exc:
        logger.warning("検索型ソース取得エラー (%s / %s): %s", source.get("name"), keyword, exc)
        return []


def fetch_feed_entries(source: dict) -> list[dict]:
    try:
        feed = feedparser.parse(source["url"])
        return [
            {
                "title": e.get("title", ""),
                "url": e.get("link", ""),
                "published": e.get("published", ""),
                "snippet": strip_html(e.get("summary", "")),
            }
            for e in feed.entries
        ]
    except Exception as exc:
        logger.warning("固定フィード取得エラー (%s): %s", source.get("name"), exc)
        return []


def filter_feed_entries_by_keyword(entries: list[dict], keyword: str, source_name: str, max_results: int) -> list[NewsItem]:
    kw = keyword.lower()
    matched = []
    for e in entries:
        if kw in e["title"].lower() or kw in e["snippet"].lower():
            matched.append(NewsItem(
                keyword=keyword,
                source=source_name,
                title=e["title"],
                url=e["url"],
                published=e["published"],
                snippet=e["snippet"],
            ))
        if len(matched) >= max_results:
            break
    return matched


def fetch_article_excerpt(url: str, max_chars: int = 500) -> str:
    try:
        resp = requests.get(url, timeout=ARTICLE_FETCH_TIMEOUT, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        paragraphs = soup.find_all("p")
        text = " ".join(p.get_text(strip=True) for p in paragraphs)
        return text[:max_chars]
    except Exception as exc:
        logger.warning("記事本文取得エラー (%s): %s", url, exc)
        return ""


def dedupe_by_url(items: list[NewsItem]) -> list[NewsItem]:
    seen = set()
    result = []
    for item in items:
        if item.url not in seen:
            seen.add(item.url)
            result.append(item)
    return result


def collect(keywords: list[str], sources: list[dict]) -> dict[str, list[NewsItem]]:
    grouped: dict[str, list[NewsItem]] = {kw: [] for kw in keywords}
    feed_cache: dict[str, list[dict]] = {}

    for source in sources:
        source_type = source.get("type")
        if source_type == "query":
            for kw in keywords:
                logger.info("[%s] %s を検索中...", source["name"], kw)
                grouped[kw].extend(fetch_query_source(source, kw, MAX_RESULTS_PER_SOURCE))
                time.sleep(FETCH_INTERVAL_SECONDS)
        elif source_type == "feed":
            if source["url"] not in feed_cache:
                logger.info("[%s] フィードを取得中...", source["name"])
                feed_cache[source["url"]] = fetch_feed_entries(source)
                time.sleep(FETCH_INTERVAL_SECONDS)
            entries = feed_cache[source["url"]]
            for kw in keywords:
                grouped[kw].extend(filter_feed_entries_by_keyword(entries, kw, source["name"], MAX_RESULTS_PER_SOURCE))
        else:
            logger.warning("未知のソースタイプをスキップ: %s", source_type)

    for kw in keywords:
        grouped[kw] = dedupe_by_url(grouped[kw])[:MAX_ARTICLES_PER_KEYWORD]

    return grouped


def build_prompt(item: NewsItem) -> str:
    body = item.body_excerpt or item.snippet
    return (
        "以下はニュース記事の情報です。内容を日本語で2〜3文程度に客観的に要約してください。\n"
        "憶測や意見を加えず、記事に書かれている事実のみをまとめてください。\n\n"
        f"タイトル: {item.title}\n"
        f"本文抜粋: {body}\n"
    )


def summarize_all(grouped: dict[str, list[NewsItem]], api_key: str, model: str) -> None:
    total = sum(len(items) for items in grouped.values())
    count = 0
    for keyword, items in grouped.items():
        for item in items:
            count += 1
            logger.info("[%d/%d] %s: %s の要約を生成中...", count, total, keyword, item.title[:40])
            item.body_excerpt = fetch_article_excerpt(item.url)
            try:
                item.summary = call_openrouter(build_prompt(item), api_key, model)
            except Exception as exc:
                item.error = f"LLM要約エラー: {exc}"
                logger.warning("%s: %s", item.title[:40], item.error)
            time.sleep(REQUEST_INTERVAL_SECONDS)


def sanitize_filename(name: str) -> str:
    invalid_chars = '\\/:*?"<>|'
    cleaned = "".join("_" if c in invalid_chars else c for c in name)
    return cleaned.strip().strip(".") or "keyword"


def save_report(grouped: dict[str, list[NewsItem]], output_dir: Path) -> list[tuple[str, Path, Path]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    generated_at = datetime.now().isoformat(timespec="seconds")
    results = []

    for keyword, items in grouped.items():
        safe_keyword = sanitize_filename(keyword)
        json_path = output_dir / f"{safe_keyword}_{timestamp}.json"
        md_path = output_dir / f"{safe_keyword}_{timestamp}.md"

        with json_path.open("w", encoding="utf-8") as f:
            json.dump(
                {
                    "keyword": keyword,
                    "disclaimer": DISCLAIMER,
                    "generated_at": generated_at,
                    "articles": [asdict(item) for item in items],
                },
                f, ensure_ascii=False, indent=2,
            )

        lines = [f"# {keyword} のニュースレポート ({datetime.now().strftime('%Y-%m-%d %H:%M')})", "", DISCLAIMER, ""]
        if not items:
            lines.append("(該当記事なし)")
        else:
            for item in items:
                lines.append(f"- **{item.title}**")
                lines.append(f"  URL: {item.url}")
                if item.error:
                    lines.append(f"  エラー: {item.error}")
                else:
                    lines.append(f"  要約: {item.summary}")
                lines.append("")
        md_path.write_text("\n".join(lines), encoding="utf-8")

        results.append((keyword, json_path, md_path))

    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OpenRouter無料LLMを使ったニュース自動収集・要約")
    base = Path(__file__).parent
    parser.add_argument("--keywords-file", type=str, default=str(base / "keywords.txt"))
    parser.add_argument("--sources-file", type=str, default=str(base / "sources.json"))
    parser.add_argument("--output-dir", type=str, default=str(base / "output"))
    parser.add_argument("--model", type=str, default=None)
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

    model = args.model or os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL)
    keywords = load_keywords(Path(args.keywords_file))
    sources = load_sources(Path(args.sources_file))

    logger.info("対象キーワード: %s", ", ".join(keywords))
    logger.info("使用モデル: %s", model)

    grouped = collect(keywords, sources)
    summarize_all(grouped, api_key, model)
    results = save_report(grouped, Path(args.output_dir))

    print()
    print(DISCLAIMER)
    for keyword, json_path, md_path in results:
        items = grouped[keyword]
        print(f"\n[{keyword}] {len(items)}件")
        print(f"  JSON: {json_path}")
        print(f"  MD  : {md_path}")
        for item in items:
            label = item.error if item.error else item.summary
            print(f"  - {item.title[:50]} -> {label}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
