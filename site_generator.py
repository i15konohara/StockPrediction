"""news_summarizer の収集結果を静的HTMLサイトとして生成するツール

output/ 内の全JSONレポートを読み込み、キーワードごとのアーカイブページと
トップページ(最新記事フィード)を含む静的サイトを docs/ に生成する。
GitHub PagesやNetlifyなど、静的ホスティングにそのままデプロイできる。

使い方:
    python collector.py          # まずニュースを収集(output/ にJSONが増える)
    python site_generator.py     # output/ の全データからサイトを再生成

注意:
    本サイトは他サイトのニュースをLLMで要約し、原文へのリンクを付けて
    紹介するアグリゲーター(まとめサイト)です。広告掲載を検討する場合、
    Google AdSense等の審査基準(独自性のある十分な価値のあるコンテンツを
    求める)を満たさない可能性がある点に注意してください。
"""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

SITE_TITLE = "ニュースまとめ"
SITE_DESCRIPTION = "キーワード別に自動収集・要約したニュースのまとめサイトです。"
LATEST_FEED_SIZE = 30

AD_SLOT = '<div class="ad-slot"><!-- 広告コード(Google AdSense等)をここに貼り付け --></div>'

DISCLAIMER = (
    "本サイトは自動収集・自動要約による参考情報のまとめです。内容の正確性は保証されません。"
    "詳細は各記事の出典元サイトをご確認ください。投資助言ではありません。"
)


@dataclass
class Article:
    keyword: str
    source: str
    title: str
    url: str
    summary: str
    collected_at: str


def load_articles(output_dir: Path) -> dict[str, list[Article]]:
    by_keyword: dict[str, dict[str, Article]] = defaultdict(dict)

    for path in sorted(output_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue

        keyword = data.get("keyword")
        if not keyword:
            continue

        for item in data.get("articles", []):
            if item.get("error") or not item.get("summary") or not item.get("url"):
                continue
            article = Article(
                keyword=keyword,
                source=item.get("source", ""),
                title=item.get("title", ""),
                url=item["url"],
                summary=item.get("summary", ""),
                collected_at=item.get("collected_at", ""),
            )
            existing = by_keyword[keyword].get(article.url)
            if existing is None or article.collected_at > existing.collected_at:
                by_keyword[keyword][article.url] = article

    return {
        kw: sorted(arts.values(), key=lambda a: a.collected_at, reverse=True)
        for kw, arts in by_keyword.items()
    }


def slugify(name: str) -> str:
    invalid_chars = '\\/:*?"<>|'
    cleaned = "".join("_" if c in invalid_chars else c for c in name)
    return cleaned.strip().strip(".") or "keyword"


def format_datetime(iso_str: str) -> str:
    try:
        return datetime.fromisoformat(iso_str).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso_str


def render_article_card(article: Article, show_keyword: bool = False) -> str:
    title = html.escape(article.title)
    summary = html.escape(article.summary)
    source = html.escape(article.source)
    url = html.escape(article.url, quote=True)
    when = html.escape(format_datetime(article.collected_at))
    keyword_tag = ""
    if show_keyword:
        keyword_slug = html.escape(slugify(article.keyword), quote=True)
        keyword_name = html.escape(article.keyword)
        keyword_tag = f'<a class="card-tag" href="keywords/{keyword_slug}.html">{keyword_name}</a>'
    return f"""
    <article class="card">
      {keyword_tag}
      <h3><a href="{url}" target="_blank" rel="noopener noreferrer nofollow">{title}</a></h3>
      <p class="card-summary">{summary}</p>
      <p class="card-meta">出典: {source} ・ {when}</p>
    </article>"""


def slugify_model(model: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", model)


def render_nav(keyword_counts: list[tuple[str, int]], asset_prefix: str) -> str:
    links = [
        f'<a href="{asset_prefix}index.html">トップ</a>',
        f'<a href="{asset_prefix}predictions.html">株価予測ログ</a>',
        f'<a href="{asset_prefix}models.html">モデル別精度</a>',
    ]
    for keyword, count in keyword_counts:
        slug = html.escape(slugify(keyword), quote=True)
        name = html.escape(keyword)
        links.append(f'<a href="{asset_prefix}keywords/{slug}.html">{name} ({count})</a>')
    return "\n".join(links)


def render_page(title: str, description: str, body: str, nav: str, asset_prefix: str) -> str:
    return f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<meta name="description" content="{html.escape(description)}">
<link rel="stylesheet" href="{asset_prefix}style.css">
</head>
<body>
<header class="site-header">
  <a class="site-title" href="{asset_prefix}index.html">{html.escape(SITE_TITLE)}</a>
  <p class="site-desc">{html.escape(SITE_DESCRIPTION)}</p>
</header>
{AD_SLOT}
<nav class="keyword-nav">{nav}</nav>
<main>
{body}
</main>
{AD_SLOT}
<footer class="site-footer">
  <p>{html.escape(DISCLAIMER)}</p>
</footer>
</body>
</html>
"""


def build_index(by_keyword: dict[str, list[Article]], site_dir: Path) -> None:
    keyword_counts = sorted(((kw, len(arts)) for kw, arts in by_keyword.items()), key=lambda x: -x[1])
    nav = render_nav(keyword_counts, asset_prefix="")

    all_articles = [a for arts in by_keyword.values() for a in arts]
    all_articles.sort(key=lambda a: a.collected_at, reverse=True)
    latest = all_articles[:LATEST_FEED_SIZE]

    cards = "\n".join(render_article_card(a, show_keyword=True) for a in latest)
    body = f"""
    <section>
      <h2>最新ニュース</h2>
      <div class="card-grid">
        {cards if cards else '<p>まだ記事がありません。collector.py を実行してください。</p>'}
      </div>
    </section>"""

    html_out = render_page(SITE_TITLE, SITE_DESCRIPTION, body, nav, asset_prefix="")
    (site_dir / "index.html").write_text(html_out, encoding="utf-8")


def build_keyword_pages(by_keyword: dict[str, list[Article]], site_dir: Path) -> None:
    keyword_counts = sorted(((kw, len(arts)) for kw, arts in by_keyword.items()), key=lambda x: -x[1])
    keywords_dir = site_dir / "keywords"
    keywords_dir.mkdir(parents=True, exist_ok=True)

    for keyword, articles in by_keyword.items():
        nav = render_nav(keyword_counts, asset_prefix="../")
        cards = "\n".join(render_article_card(a) for a in articles)
        body = f"""
        <section>
          <h2>{html.escape(keyword)} の記事一覧({len(articles)}件)</h2>
          <div class="card-grid">
            {cards}
          </div>
        </section>"""
        title = f"{keyword} のニュースまとめ | {SITE_TITLE}"
        html_out = render_page(title, f"{keyword}に関するニュースの自動収集・要約アーカイブ", body, nav, asset_prefix="../")
        slug = slugify(keyword)
        (keywords_dir / f"{slug}.html").write_text(html_out, encoding="utf-8")


OUTCOME_LABELS = {
    "hit": "✅ 的中", "miss": "❌ 不的中", "no_data": "⚠️ データなし",
    "tie": "🤝 票が同数(判定不可)", "": "⏳ 判定待ち",
}
DIRECTION_LABELS = {"positive": "📈 ポジティブ", "negative": "📉 ネガティブ", "tie": "🤝 票が同数"}


def load_predictions(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    try:
        return json.loads(log_path.read_text(encoding="utf-8"))
    except Exception:
        return []


def compute_accuracy(records: list[dict]) -> dict:
    evaluated = [r for r in records if r.get("outcome") in ("hit", "miss")]
    total = len(evaluated)
    hits = sum(1 for r in evaluated if r["outcome"] == "hit")
    rate = round(hits / total * 100, 1) if total else None
    return {"total": total, "hits": hits, "rate": rate}


def render_accuracy_stat(label: str, stats: dict) -> str:
    if stats["total"] == 0:
        return f"<li>{html.escape(label)}: 判定待ちのため集計なし</li>"
    return f"<li>{html.escape(label)}: 的中率 {stats['rate']}%({stats['hits']}/{stats['total']}件)</li>"


def render_prediction_row(rec: dict, asset_prefix: str = "") -> str:
    """1モデル単体の予測1件を表示する行(reasonは単一文字列)"""
    keyword_slug = html.escape(slugify(rec.get("keyword", "")), quote=True)
    keyword = html.escape(rec.get("keyword", ""))
    market = html.escape(rec.get("market_label", rec.get("market", "")))
    horizon = html.escape(rec.get("horizon_label", rec.get("horizon", "")))
    direction = html.escape(DIRECTION_LABELS.get(rec.get("direction"), rec.get("direction", "")))
    reason = html.escape(rec.get("reason", ""))
    outcome = html.escape(OUTCOME_LABELS.get(rec.get("outcome", ""), rec.get("outcome", "")))
    predicted_at = html.escape(format_datetime(rec.get("predicted_at", "")))
    target_date = html.escape(rec.get("target_date", ""))
    change = rec.get("actual_change_pct")
    change_str = html.escape(f"{change:+.2f}%" if change is not None else "-")
    return f"""
    <tr>
      <td>{predicted_at}</td>
      <td><a href="{asset_prefix}keywords/{keyword_slug}.html">{keyword}</a></td>
      <td>{market}</td>
      <td>{horizon}<br><span class="card-meta">対象日: {target_date}</span></td>
      <td>{direction}</td>
      <td class="reason-cell">{reason}</td>
      <td>{outcome}</td>
      <td>{change_str}</td>
    </tr>"""


def render_consensus_row(rec: dict, asset_prefix: str = "") -> str:
    """複数モデルの多数決による予測1件を表示する行(votes/reasonsは辞書)"""
    keyword_slug = html.escape(slugify(rec.get("keyword", "")), quote=True)
    keyword = html.escape(rec.get("keyword", ""))
    market = html.escape(rec.get("market_label", rec.get("market", "")))
    horizon = html.escape(rec.get("horizon_label", rec.get("horizon", "")))
    direction = html.escape(DIRECTION_LABELS.get(rec.get("direction"), rec.get("direction", "")))
    outcome = html.escape(OUTCOME_LABELS.get(rec.get("outcome", ""), rec.get("outcome", "")))
    predicted_at = html.escape(format_datetime(rec.get("predicted_at", "")))
    target_date = html.escape(rec.get("target_date", ""))
    change = rec.get("actual_change_pct")
    change_str = html.escape(f"{change:+.2f}%" if change is not None else "-")

    votes = rec.get("votes", {})
    vote_str = html.escape(f"👍{votes.get('positive', 0)} / 👎{votes.get('negative', 0)}(全{votes.get('total', 0)}モデル)")

    reasons = rec.get("reasons", {})
    reason_items = "".join(
        f"<li><strong>{html.escape(model)}:</strong> {html.escape(reason)}</li>"
        for model, reason in reasons.items()
    )
    reason_html = f"<ul class='reason-list'>{reason_items}</ul>" if reason_items else ""

    return f"""
    <tr>
      <td>{predicted_at}</td>
      <td><a href="{asset_prefix}keywords/{keyword_slug}.html">{keyword}</a></td>
      <td>{market}</td>
      <td>{horizon}<br><span class="card-meta">対象日: {target_date}</span></td>
      <td>{direction}<br><span class="card-meta">{vote_str}</span></td>
      <td class="reason-cell">{reason_html}</td>
      <td>{outcome}</td>
      <td>{change_str}</td>
    </tr>"""


def build_predictions_page(log_path: Path, site_dir: Path, keyword_counts: list[tuple[str, int]]) -> None:
    records = load_predictions(log_path)
    records_sorted = sorted(records, key=lambda r: r.get("predicted_at", ""), reverse=True)

    overall = compute_accuracy(records_sorted)
    japan_stats = compute_accuracy([r for r in records_sorted if r.get("market") == "japan"])
    us_stats = compute_accuracy([r for r in records_sorted if r.get("market") == "us"])

    stats_html = "<ul class='accuracy-list'>" + "".join([
        render_accuracy_stat("総合", overall),
        render_accuracy_stat("日本株(日経平均)", japan_stats),
        render_accuracy_stat("米国株(S&P500)", us_stats),
    ]) + "</ul>"

    rows = "\n".join(render_consensus_row(r) for r in records_sorted)
    body = f"""
    <section>
      <h2>株価予測ログと的中率(複数モデルの多数決)</h2>
      <p>複数のLLMモデル(<a href="models.html">モデル一覧</a>)がそれぞれ独立に予測し、
      多数決で最終判定した結果です。予測対象日を過ぎたものは、実際の指数(日本株: 日経平均
      ^N225、米国株: S&amp;P500 ^GSPC)の値動きと比較して自動的に答え合わせしています。
      予測日時が新しい順に表示しています。</p>
      {stats_html}
      <div class="table-wrap">
      <table class="prediction-table">
        <thead>
          <tr>
            <th>予測日時</th><th>キーワード</th><th>市場</th><th>期間</th>
            <th>多数決の予測</th><th>各モデルの理由</th><th>結果</th><th>実際の変化率</th>
          </tr>
        </thead>
        <tbody>
        {rows if rows else '<tr><td colspan="8">まだ予測がありません。predictor.py を実行してください。</td></tr>'}
        </tbody>
      </table>
      </div>
    </section>"""

    nav = render_nav(keyword_counts, asset_prefix="")
    html_out = render_page(
        f"株価予測ログと的中率 | {SITE_TITLE}",
        "複数のLLMモデルによる株価予測の多数決と的中率の記録(日本株・米国株、明日・1週間後・1か月後)",
        body, nav, asset_prefix="",
    )
    (site_dir / "predictions.html").write_text(html_out, encoding="utf-8")


def render_model_stat_row(model: str, records: list[dict]) -> str:
    acc = compute_accuracy(records)
    slug = html.escape(slugify_model(model), quote=True)
    name = html.escape(model)
    count = len(records)
    rate_str = f"{acc['rate']}%({acc['hits']}/{acc['total']}件)" if acc["total"] else "判定待ち"
    return f"""
    <tr>
      <td><a href="models/{slug}.html">{name}</a></td>
      <td>{count}件</td>
      <td>{html.escape(rate_str)}</td>
    </tr>"""


def build_models_page(by_model_dir: Path, site_dir: Path, keyword_counts: list[tuple[str, int]], models: list[str]) -> None:
    rows = "\n".join(
        render_model_stat_row(m, load_predictions(by_model_dir / f"{slugify_model(m)}.json")) for m in models
    )
    body = f"""
    <section>
      <h2>モデル別 予測精度比較</h2>
      <p>複数のLLMモデルがそれぞれ独立に株価予測を行い、多数決で最終予測(<a href="predictions.html">株価予測ログ</a>)
      を決定しています。ここでは各モデル単体の予測精度を比較できます。</p>
      <div class="table-wrap">
      <table class="prediction-table">
        <thead><tr><th>モデル</th><th>予測件数</th><th>的中率</th></tr></thead>
        <tbody>
        {rows if rows else '<tr><td colspan="3">まだ予測がありません。predictor.py を実行してください。</td></tr>'}
        </tbody>
      </table>
      </div>
    </section>"""
    nav = render_nav(keyword_counts, asset_prefix="")
    html_out = render_page(
        f"モデル別予測精度 | {SITE_TITLE}",
        "複数のLLMモデルによる株価予測の精度比較",
        body, nav, asset_prefix="",
    )
    (site_dir / "models.html").write_text(html_out, encoding="utf-8")


def build_model_detail_pages(by_model_dir: Path, site_dir: Path, keyword_counts: list[tuple[str, int]], models: list[str]) -> None:
    models_dir = site_dir / "models"
    models_dir.mkdir(parents=True, exist_ok=True)

    for model in models:
        records = load_predictions(by_model_dir / f"{slugify_model(model)}.json")
        records_sorted = sorted(records, key=lambda r: r.get("predicted_at", ""), reverse=True)
        stats_html = "<ul class='accuracy-list'>" + render_accuracy_stat("このモデル", compute_accuracy(records_sorted)) + "</ul>"
        rows = "\n".join(render_prediction_row(r, asset_prefix="../") for r in records_sorted)
        body = f"""
        <section>
          <h2>{html.escape(model)} の予測ログ</h2>
          {stats_html}
          <div class="table-wrap">
          <table class="prediction-table">
            <thead>
              <tr>
                <th>予測日時</th><th>キーワード</th><th>市場</th><th>期間</th>
                <th>予測</th><th>理由</th><th>結果</th><th>実際の変化率</th>
              </tr>
            </thead>
            <tbody>
            {rows if rows else '<tr><td colspan="8">まだ予測がありません。</td></tr>'}
            </tbody>
          </table>
          </div>
        </section>"""
        nav = render_nav(keyword_counts, asset_prefix="../")
        html_out = render_page(
            f"{model} の予測ログ | {SITE_TITLE}",
            f"{model}による株価予測の記録",
            body, nav, asset_prefix="../",
        )
        slug = slugify_model(model)
        (models_dir / f"{slug}.html").write_text(html_out, encoding="utf-8")


def write_static_assets(site_dir: Path) -> None:
    css = """
:root {
  --bg: #f7f7f5;
  --card-bg: #ffffff;
  --text: #1f2328;
  --muted: #6b7280;
  --accent: #1a56db;
  --border: #e5e7eb;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Hiragino Sans", "Yu Gothic", sans-serif;
  background: var(--bg);
  color: var(--text);
  line-height: 1.7;
}
.site-header {
  padding: 24px 16px;
  text-align: center;
  border-bottom: 1px solid var(--border);
  background: var(--card-bg);
}
.site-title {
  font-size: 1.6rem;
  font-weight: 700;
  color: var(--text);
  text-decoration: none;
}
.site-desc { color: var(--muted); margin: 8px 0 0; font-size: 0.9rem; }
.keyword-nav {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  justify-content: center;
  padding: 12px 16px;
  max-width: 960px;
  margin: 0 auto;
}
.keyword-nav a {
  padding: 6px 12px;
  border-radius: 999px;
  background: var(--card-bg);
  border: 1px solid var(--border);
  color: var(--text);
  text-decoration: none;
  font-size: 0.85rem;
  white-space: nowrap;
}
.keyword-nav a:hover { border-color: var(--accent); color: var(--accent); }
main { max-width: 960px; margin: 0 auto; padding: 8px 16px 32px; }
h2 { font-size: 1.2rem; margin: 24px 0 12px; }
.card-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 16px;
}
.card {
  background: var(--card-bg);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 16px;
}
.card h3 { margin: 4px 0 8px; font-size: 1rem; line-height: 1.5; }
.card h3 a { color: var(--text); text-decoration: none; }
.card h3 a:hover { color: var(--accent); text-decoration: underline; }
.card-summary { font-size: 0.9rem; color: var(--text); margin: 0 0 8px; }
.card-meta { font-size: 0.78rem; color: var(--muted); margin: 0; }
.card-tag {
  display: inline-block;
  font-size: 0.72rem;
  background: #eef2ff;
  color: var(--accent);
  padding: 2px 8px;
  border-radius: 999px;
  text-decoration: none;
  margin-bottom: 6px;
}
.ad-slot {
  max-width: 960px;
  margin: 16px auto;
  min-height: 60px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--muted);
  font-size: 0.75rem;
  border: 1px dashed var(--border);
}
.site-footer {
  max-width: 960px;
  margin: 0 auto;
  padding: 16px;
  color: var(--muted);
  font-size: 0.78rem;
  border-top: 1px solid var(--border);
}
.accuracy-list {
  list-style: none;
  padding: 0;
  margin: 0 0 16px;
  display: flex;
  flex-wrap: wrap;
  gap: 8px 20px;
  font-size: 0.9rem;
}
.table-wrap { overflow-x: auto; }
.prediction-table {
  width: 100%;
  border-collapse: collapse;
  background: var(--card-bg);
  font-size: 0.85rem;
  min-width: 720px;
}
.prediction-table th, .prediction-table td {
  border-bottom: 1px solid var(--border);
  padding: 8px 10px;
  text-align: left;
  vertical-align: top;
}
.prediction-table th {
  background: #f3f4f6;
  white-space: nowrap;
}
.prediction-table .reason-cell { max-width: 360px; }
.reason-list { list-style: none; margin: 0; padding: 0; }
.reason-list li { margin-bottom: 6px; }
.reason-list li:last-child { margin-bottom: 0; }
"""
    (site_dir / "style.css").write_text(css, encoding="utf-8")


def write_seo_files(
    by_keyword: dict[str, list[Article]], site_dir: Path, base_url: str, models: list[str]
) -> None:
    urls = [f"{base_url}/index.html", f"{base_url}/predictions.html", f"{base_url}/models.html"]
    urls += [f"{base_url}/keywords/{slugify(kw)}.html" for kw in by_keyword]
    urls += [f"{base_url}/models/{slugify_model(m)}.html" for m in models]
    sitemap = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for u in urls:
        sitemap.append(f"  <url><loc>{html.escape(u)}</loc></url>")
    sitemap.append("</urlset>")
    (site_dir / "sitemap.xml").write_text("\n".join(sitemap), encoding="utf-8")

    robots = f"User-agent: *\nAllow: /\nSitemap: {base_url}/sitemap.xml\n"
    (site_dir / "robots.txt").write_text(robots, encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="news_summarizerの収集結果から静的サイトを生成")
    base = Path(__file__).parent
    parser.add_argument("--output-dir", type=str, default=str(base / "output"))
    parser.add_argument("--site-dir", type=str, default=str(base / "docs"))
    parser.add_argument("--predictions-log", type=str, default=str(base / "predictions" / "log.json"))
    parser.add_argument("--by-model-dir", type=str, default=str(base / "predictions" / "by_model"))
    parser.add_argument("--models-file", type=str, default=str(base / "prediction_models.json"))
    parser.add_argument("--base-url", type=str, default="https://example.com",
                         help="デプロイ先のURL(sitemap.xml生成用)。実際のURLに変更してください")
    return parser.parse_args()


def load_models_list(path: Path) -> list[str]:
    if path.exists():
        try:
            models = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(models, list) and models:
                return [str(m) for m in models]
        except Exception:
            pass
    return []


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir)
    site_dir = Path(args.site_dir)
    predictions_log = Path(args.predictions_log)
    by_model_dir = Path(args.by_model_dir)

    if not output_dir.exists():
        print(f"出力元フォルダが見つかりません: {output_dir}")
        return 1

    by_keyword = load_articles(output_dir)
    site_dir.mkdir(parents=True, exist_ok=True)

    keyword_counts = sorted(((kw, len(arts)) for kw, arts in by_keyword.items()), key=lambda x: -x[1])

    models = load_models_list(Path(args.models_file))
    if not models and by_model_dir.exists():
        models = [p.stem for p in by_model_dir.glob("*.json")]

    write_static_assets(site_dir)
    build_index(by_keyword, site_dir)
    build_keyword_pages(by_keyword, site_dir)
    build_predictions_page(predictions_log, site_dir, keyword_counts)
    build_models_page(by_model_dir, site_dir, keyword_counts, models)
    build_model_detail_pages(by_model_dir, site_dir, keyword_counts, models)
    write_seo_files(by_keyword, site_dir, args.base_url.rstrip("/"), models)

    total_articles = sum(len(v) for v in by_keyword.values())
    print(f"サイトを生成しました: {site_dir}")
    print(f"キーワード数: {len(by_keyword)} / 記事数(重複除去後): {total_articles}")
    print(f"確認するには {site_dir / 'index.html'} をブラウザで開いてください。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
