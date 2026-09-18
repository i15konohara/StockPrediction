# ニュース自動収集・要約システム 設計書

## 1. 概要

株価に影響を与えうる企業名・人物名・イベント名などのキーワードを外部ファイルで管理し、複数のニュースソース(RSS)から関連記事を収集、OpenRouterの無料LLMモデルで要約して「タイトル・URL・要約」の3項目にまとめて出力するツール。

- OS: Windows
- 言語: Python 3.x
- LLM: OpenRouter 無料モデル(例: `google/gemini-2.5-flash:free`。無料モデルはOpenRouter側の提供状況で変わるため `.env` で切り替え可能にする)

## 2. ファイル構成

```
news_summarizer/
├── design_document.md      # 本設計書
├── collector.py            # ニュース収集・要約のメインスクリプト
├── predictor.py            # 株価予測の生成・的中判定
├── site_generator.py       # output/ + predictions/ から静的Webサイトを生成
├── openrouter_client.py    # OpenRouter API呼び出しの共通処理(collector.py/predictor.pyが共用)
├── keywords.txt            # 収集対象キーワード(1行1件、# でコメント可)
├── sources.json            # ニュースソース定義
├── prediction_models.json  # 株価予測に使う複数のLLMモデル一覧
├── requirements.txt        # 依存ライブラリ
├── .env.example            # APIキー設定テンプレート
├── .env                    # 実際のAPIキー(Git管理対象外)
├── run_all.bat             # collector→predictor→site_generatorを順に実行(タスクスケジューラ用)
├── output/
│   └── <キーワード>_YYYYMMDD_HHMMSS.json / .md   # キーワードごとの実行結果(取得日時つき)
├── calendar_info.py         # 六曜・一粒万倍日・天赦日など伝統的な暦注の算出
├── calendar_predictor.py    # 暦注(ニュース不使用)に基づく株価予測の生成・的中判定
├── predictions/
│   ├── log.json             # 複数モデルの多数決による最終予測ログ(的中判定つき、追記型)
│   ├── calendar_log.json    # 暦注に基づく多数決予測ログ(ニュースとは独立、追記型)
│   ├── calendar_by_model/
│   │   └── <モデル名>.json  # 暦注予測のモデルごとの個別ログ(追記型)
│   ├── by_model/
│   │   └── <モデル名>.json  # モデルごとの個別予測ログ(的中判定つき、追記型)
│   └── context/
│       └── <キーワード>_<1day|1week|1month>.md
│                             # 過去のニュース要約をさらに要約した文脈キャッシュ(上書き型)
└── docs/                    # site_generator.py が生成する静的サイト(GitHub Pages等にデプロイ可能)
    ├── index.html             # 全キーワード横断の最新記事フィード
    ├── predictions.html       # 多数決による株価予測ログと的中率(時系列表示)
    ├── calendar.html          # 暦注(六曜等)に基づく株価予測ログ(ニュースとは独立、実験的)
    ├── models.html            # モデル別の予測精度比較
    ├── models/<モデル名>.html # モデルごとの個別予測ログ(時系列表示)
    ├── style.css
    ├── sitemap.xml / robots.txt
    └── keywords/<キーワード>.html   # キーワードごとの記事アーカイブ(過去分を累積・重複除去)
```

## 3. 設定ファイル仕様

### 3.1 keywords.txt

1行1キーワード。空行および `#` で始まる行は無視する。

```
# keywords.txt の例
トヨタ
Microsoft
トランプ
高市早苗
イラン情勢
ウクライナ戦争
FOMC
```

### 3.2 sources.json

ニュースソースを2種類に分類して定義する。

| 種別(`type`) | 説明 | 例 |
|---|---|---|
| `query` | URLにキーワードを埋め込んで検索できるソース | Google News RSS, Bing News RSS |
| `feed` | キーワード検索機能を持たない固定カテゴリRSS | NHKニュース RSS, Yahoo!ニュース カテゴリRSS |

```json
[
  {
    "name": "Google News",
    "type": "query",
    "url_template": "https://news.google.com/rss/search?q={query}&hl=ja&gl=JP&ceid=JP:ja"
  },
  {
    "name": "Bing News",
    "type": "query",
    "url_template": "https://www.bing.com/news/search?q={query}&format=RSS"
  },
  {
    "name": "NHKニュース 経済",
    "type": "feed",
    "url": "https://www3.nhk.or.jp/rss/news/cat5.xml"
  },
  {
    "name": "Yahoo!ニュース 経済",
    "type": "feed",
    "url": "https://news.yahoo.co.jp/rss/categories/business.xml"
  }
]
```

`query`型はキーワードごとにリクエストする。`feed`型は実行につき1回だけ取得してキャッシュし、キーワードごとにタイトル/スニペットへの部分一致でローカルフィルタする(同じフィードをキーワード数だけ再取得しない)。

### 3.3 prediction_models.json

株価予測(`predictor.py`)で使用する複数のLLMモデルを列挙する。同数決着(引き分け)を避けるため、奇数個のモデルを指定することを推奨する。

```json
[
  "google/gemini-2.5-flash:free",
  "meta-llama/llama-3.3-70b-instruct:free",
  "nvidia/nemotron-3.5-lightning:free"
]
```

各モデルは同じニュース要約に対して独立に予測を行い、モデル間の多数決で最終予測(ポジティブ/ネガティブ)を決定する。ファイルが存在しない場合は上記と同じ内容がデフォルト値として使われる。

### 3.4 .env

```
OPENROUTER_API_KEY=sk-or-v1-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
OPENROUTER_MODEL=google/gemini-2.5-flash:free
```

## 4. データモデル

```python
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
    collected_at: str
    error: str = ""
```

## 5. 処理フロー

1. **初期化**
   `.env` からAPIキー・モデル名を読み込み(`python-dotenv`)、`keywords.txt` からキーワード一覧、`sources.json` からソース定義を読み込む。

2. **ニュース収集**
   - `query`型ソース: キーワードごとに `url_template` の `{query}` をURLエンコードした値で置換してリクエストし、`feedparser` でパース。上位N件(既定5件)を取得。
   - `feed`型ソース: 実行につき1回だけ取得・パースしてキャッシュ。各キーワードについて、タイトル/スニペットに部分一致する記事のみ抽出。

3. **記事本文の補完(ベストエフォート)**
   各記事URLに `requests` + `BeautifulSoup` でアクセスし、本文冒頭の一部(数百文字)を抽出。取得に失敗した場合はRSSのスニペットのみで後続処理を続行する(全体を止めない)。

4. **重複排除**
   キーワード単位でURLを基準に重複除去(同じ記事が複数ソースでヒットする場合があるため)。

5. **LLM要約(OpenRouter)**
   記事ごとに「タイトル+本文抜粋(またはスニペット)」をプロンプトに渡し、OpenRouterの無料モデルで日本語2〜3文の要約を生成する。429(レート制限)時は `Retry-After` に従って待機・再試行し、リクエスト間に一定間隔を空ける。

6. **出力**
   キーワードごとに個別のファイルとして「タイトル・URL・要約」をJSON(構造化データ)とMarkdown(閲覧用)の両方で `output/` に保存する。ファイル名は `<キーワード>_<取得日時YYYYMMDD_HHMMSS>.json/.md` とし、いつ収集したニュースかを一目で分かるようにする(ファイル名に使えない記号はアンダースコアに置換)。実行結果はコンソールにも整形表示する。

7. **定期実行**
   引数なしで `keywords.txt` と `sources.json` を読むだけで完結させ、Windowsタスクスケジューラから `python collector.py` を呼び出すだけで自動収集できるようにする。

8. **Webサイト生成(site_generator.py)**
   `output/` 内の全JSONを読み込み、記事をキーワードごとにURL基準で重複除去して集約する(同じ記事が複数回の実行で再取得されても1件にまとめる)。収集日時降順でソートし、全キーワード横断の最新記事フィード(トップページ)とキーワードごとのアーカイブページを静的HTMLとして `docs/` に生成する。`collector.py` とは独立して実行し、既存の `output/` データが増えるたびに再生成することでサイトを更新する。広告枠は `<div class="ad-slot">` としてプレースホルダー化し、Google AdSense等のコードを後から埋め込めるようにする。

9. **複数モデルによる株価予測の生成(predictor.py)**
   `prediction_models.json` に列挙した各モデルについて、キーワードごとに直近(最新の`generated_at`)のニュース要約群を1つのプロンプトにまとめ、OpenRouterに「日本株式市場全体」「米国株式市場全体」への影響を、明日・1週間後・1か月後の3期間についてポジティブ/ネガティブと理由付きで独立に判定させる(JSON形式で応答させ、`{market: {horizon: {direction, reason}}}` を抽出)。LLMの応答が期待するJSON形式にならない場合は1回だけ再試行し、それでも失敗した場合はそのモデル・キーワードの組をスキップして処理全体は継続する。同じニュースセット(`keyword` + `generated_at`の組)に対して、モデルごとに重複して予測を作らない。
   モデルごとの予測は `predictions/by_model/<モデル名>.json` に個別のファイルとして追記保存する(過去の予測は保持し、蓄積型のログとする)。予測ごとに、生成時点の参照指数(日本株: 日経平均 `^N225`、米国株: S&P500 `^GSPC`、yfinanceで取得)の終値を基準値(`baseline_price`)として記録する。

   プロンプトには、今回の最新ニュース要約に加えて以下の2種類のコンテキストを含め、モデルが短期的なノイズだけでなく中長期の流れも踏まえて判定できるようにする。

   - **ニュースの文脈**: キーワードごとに、直近1日・1週間・1か月以内に生成された過去の `output/` レポートの記事要約をまとめて1つのLLMプロンプトに渡し、「この期間全体としてどのような出来事・流れがあったか」を3〜5文程度にさらに要約させる(いわゆる要約の要約)。結果は `predictions/context/<キーワード>_<1day|1week|1month>.md` にYAML frontmatter(`source_generated_at`等)付きで保存し、該当キーワードの最新レポートが前回生成時から変化していなければ再生成せずキャッシュを再利用する(モデル数に依存せずキーワードあたり最大3回のLLM呼び出しで済む)。要約に使うモデルは `prediction_models.json` の先頭のモデルを用いる。
   - **株価の推移**: 日本株(`^N225`)・米国株(`^GSPC`)それぞれについて、yfinanceの日足OHLCV(直近3か月分)から直近1日・1週間・1か月の「直近終値・期間騰落率(%)・期間高値/安値・直近取引日の始値高値安値終値」をテキスト化する。単純な寄付・大引けの2点比較ではなく、トレンド(騰落率)とボラティリティ(高値安値レンジ)の両方を伝えることで、値動きの勢いや振れ幅を予測材料に含められる。この計算は実行につき1回だけ行い、全キーワード・全モデルで共有する。

   プロンプト内では、明日の予測には「直近1日」、1週間後の予測には「直近1週間」、1か月後の予測には「直近1か月」のニュースの文脈・株価の推移をそれぞれ重視するようモデルに指示する。

10. **多数決による最終予測の算出(predictor.py)**
    同一の `keyword` + `source_generated_at` + `market` + `horizon` の組について、各モデルが出した方向(positive/negative)の票を集計する。ポジティブ票が多ければ`positive`、ネガティブ票が多ければ`negative`、同数の場合は`tie`(多数決不成立)とし、各モデルの理由(`reasons`: モデル名→理由の辞書)と内訳(`votes`: positive/negative/total件数)を添えて `predictions/log.json` に追記保存する。

11. **株価予測の的中判定(predictor.py)**
    `predictor.py` 実行時、モデルごとのログ・多数決ログの両方について、未判定(`outcome`が空)の予測のうち対象日(`target_date`)が当日以前になったものを、対象日に最も近い営業日の指数終値(yfinance)と比較して答え合わせする。基準値からの変化率を計算し、実際の方向(上昇→positive/下落→negative)と予測方向が一致すれば `hit`(的中)、不一致なら `miss`(不的中)とする。データ取得に失敗した場合は `no_data`、多数決が`tie`の場合は評価対象外として `tie` のままとする。これにより、モデルごとの予測精度と多数決の予測精度をそれぞれ独立に追跡できる。

12. **予測ログの時系列表示(site_generator.py)**
    `predictions/log.json`(多数決)を読み込み、予測日時が新しい順にテーブル形式で一覧表示する `docs/predictions.html` を生成する。各行に「予測日時・キーワード・市場・期間(対象日)・多数決の予測(内訳つき)・各モデルの理由・結果(的中/不的中/票同数/判定待ち/データなし)・実際の変化率」を表示し、ページ上部に総合・日本株・米国株それぞれの的中率を集計して表示する。
    また `predictions/by_model/` の各ファイルを読み込み、モデルごとの予測精度比較ページ `docs/models.html` と、モデルごとの個別予測ログを時系列表示する `docs/models/<モデル名>.html` を生成する。サイト内の全ページのナビゲーションに「株価予測ログ」「モデル別精度」へのリンクを追加する。

13. **暦注(六曜・一粒万倍日・天赦日)とニュースを組み合わせた株価予測(calendar_info.py / calendar_predictor.py)**
    `predictor.py` のログとは別系統の実験的な機能。`calendar_info.py` が、旧暦(lunardateで算出)をもとにした六曜、二十四節気の節切りをもとにした一粒万倍日、季節と干支の組み合わせをもとにした天赦日を計算する(いずれも外部の暦サイトの実データと突き合わせて算出式を検証済み)。`calendar_predictor.py` は、本日・明日の暦注(六曜・一粒万倍日・天赦日・干支・季節区分)と、`output/` にある全キーワードの直近のニュース要約の両方をプロンプトに含め、`prediction_models.json` の各モデルに「明日」の日本株式市場・米国株式市場への影響を独立に予測させ、暦注とニュースそれぞれがどう判断に影響したかを理由に含めさせる(1週間後・1か月後は対象外。暦注の吉凶が薄まり考察の意味が乏しいため)。予測・多数決・的中判定の仕組み(基準値取得・答え合わせ)は `predictor.py` の実装を共有する。ログは `predictions/calendar_log.json`(多数決)・`predictions/calendar_by_model/<モデル名>.json`(モデル別)に別系統で保存し、`site_generator.py` が `docs/calendar.html` という独立したページとして表示する(ナビゲーションに「暦注予測(実験)」へのリンクを追加)。これらの暦注は科学的根拠のない伝統的な考え方であり、実際の相場変動との因果関係は確認されていないことをページ内に明記する。

## 6. 外部ライブラリ

| ライブラリ | 用途 |
|---|---|
| `requests` | HTTP通信(記事本文取得、OpenRouter API呼び出し) |
| `feedparser` | RSSフィードのパース(Google News / Bing News / NHK / Yahoo!ニュース) |
| `beautifulsoup4` + `lxml` | 記事本文の簡易抽出(スクレイピング) |
| `yfinance` | 株価予測の答え合わせ用に日経平均・S&P500の実際の終値を取得(predictor.py) |
| `python-dotenv` | `.env` からのAPIキー読み込み |
| `lunardate` | 旧暦(太陰太陽暦)の月日への変換(calendar_info.py、六曜の算出に使用) |
| `koyomi` | 太陽黄経の算出(calendar_info.py、二十四節気・節切りの判定に使用) |

## 7. 出力フォーマット

キーワードごとに `<キーワード>_<YYYYMMDD_HHMMSS>.json` と `.md` の2ファイルを出力する(例: `トヨタ_20260910_111556.json`)。

### JSON例(`トヨタ_20260910_111556.json`)

```json
{
  "keyword": "トヨタ",
  "disclaimer": "本レポートは自動生成された参考情報です。内容の正確性は保証されません。",
  "generated_at": "2026-09-10T11:15:56",
  "articles": [
    {
      "source": "Google News",
      "title": "トヨタ、新型EVを発表",
      "url": "https://example.com/article1",
      "summary": "トヨタ自動車は新型EVモデルを発表した。..."
    }
  ]
}
```

### Markdown例(`トヨタ_20260910_111556.md`)

```
# トヨタ のニュースレポート (2026-09-10 11:15)

- **トヨタ、新型EVを発表**
  URL: https://example.com/article1
  要約: トヨタ自動車は新型EVモデルを発表した。...
```

## 8. エラーハンドリング・レート制限方針

- ソース単位・記事単位で例外を捕捉し、1件の失敗が全体を止めないようにする(`error` フィールドに記録)。
- OpenRouter APIの429エラーは指数バックオフ + `Retry-After` ヘッダ尊重でリトライ(最大3回)。
- 記事本文取得の失敗はログに警告を出し、スニペットのみで要約を続行する。
- ソースRSSのURLは提供元の都合で変更・廃止される可能性があるため、`sources.json` を編集するだけで差し替えられる設計とする。

## 9. 免責事項

生成される要約はニュース記事の客観的な内容整理を目的とした自動生成コンテンツであり、投資助言ではない。出力には毎回ディスクレイマーを含める。

## 10. 今後の運用(Windowsタスクスケジューラでの自動実行)

```
プログラム: C:\Program Files\Python312\python.exe
引数: D:\ClaudeCodeDir\news_summarizer\collector.py
開始場所: D:\ClaudeCodeDir\news_summarizer
```

上記をタスクスケジューラに登録し、任意の間隔(例: 1日1回)で自動実行する。
