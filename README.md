# news_summarizer

キーワード(企業名・人物名・イベント名など)ごとに複数のニュースソース(RSS)から関連記事を収集し、OpenRouterの無料LLMモデルで日本語要約する自動ニュース収集ツールです。結果は「タイトル・URL・要約」の3項目としてキーワードごとにJSON/Markdownで保存されます。

詳細な設計は [design_document.md](design_document.md) を参照してください。

## セットアップ

```
cd news_summarizer
pip install -r requirements.txt
copy .env.example .env
```

`.env` を編集し、OpenRouterのAPIキーを設定します。

```
OPENROUTER_API_KEY=sk-or-v1-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
OPENROUTER_MODEL=google/gemini-2.5-flash:free
```

APIキーは [openrouter.ai/keys](https://openrouter.ai/keys) から取得できます。無料モデルはOpenRouter側の提供状況で変わることがあるため、[openrouter.ai/models](https://openrouter.ai/models) で `:free` サフィックスのモデルを確認し、必要に応じて `OPENROUTER_MODEL` を変更してください。

## キーワードを編集する

[keywords.txt](keywords.txt) に1行1キーワードで記述します。`#` で始まる行はコメント(除外)として扱われます。

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

企業名・人物名・イベント名など、株価に影響を与えうる任意のキーワードを自由に追加・コメントアウトできます。

## ニュースソースを編集する

[sources.json](sources.json) で収集元を管理します。ソースには2種類あります。

| 種別(`type`) | 説明 | デフォルトの例 |
|---|---|---|
| `query` | キーワードで検索できるソース | Google News, Bing News |
| `feed` | キーワード検索機能を持たない固定カテゴリRSS(実行ごとに1回だけ取得し、キーワードでローカルフィルタする) | NHKニュース 経済, Yahoo!ニュース 経済 |

ソースの追加・削除・差し替えは `sources.json` を編集するだけで反映されます。NHK/Yahoo!のRSS URLは提供元の都合で変更・廃止されることがあるため、動かなくなった場合はURLを見直してください。

## 実行する

```
python collector.py
```

### オプション

```
python collector.py --keywords-file 別のキーワードファイル.txt
python collector.py --sources-file 別のソース定義.json
python collector.py --output-dir 保存先フォルダ
python collector.py --model "別のモデルID"
```

## 出力

キーワードごとに、取得日時をファイル名に含む以下の2ファイルが `output/` に生成されます(例: `トヨタ_20260910_111556.json` / `.md`)。

- `<キーワード>_<YYYYMMDD_HHMMSS>.json` — 構造化データ(タイトル・URL・要約・ソース名など)
- `<キーワード>_<YYYYMMDD_HHMMSS>.md` — 閲覧用のMarkdownレポート

実行結果はコンソールにも一覧表示されます。出力には毎回ディスクレイマー(「本レポートは自動生成された参考情報です。内容の正確性は保証されません。投資助言ではありません。」)が付与されます。

## 処理の流れ(概要)

1. キーワードごとにGoogle News / Bing NewsのRSSを検索
2. NHK / Yahoo!ニュースの固定フィードを取得し、キーワードでローカルフィルタ
3. 記事URLへアクセスし、本文の一部をベストエフォートで取得(失敗時はRSSのスニペットのみで続行)
4. 同一URLの記事を重複除去(キーワードあたり最大8件)
5. OpenRouterの無料LLMで記事ごとに2〜3文の日本語要約を生成
6. キーワードごとにJSON/Markdownで保存

## 注意点

- キーワード数 × ソース数の分だけ処理が発生するため、キーワードが多いと実行に数分かかります。OpenRouterへの要約リクエストは既定で4秒間隔を空けています(`OPENROUTER_REQUEST_INTERVAL` 環境変数で調整可能)。
- Bing NewsなどのRSSは応答が遅い/ブロックされることがありますが、その場合はそのソースだけ結果が空になり、処理全体は止まりません。
- レート制限(HTTP 429)やLLMからの空応答は自動的にリトライします(最大3回)。
- 1記事の取得・要約に失敗しても、その記事だけエラーとして記録し、処理全体は継続します。

## 定期実行(Windowsタスクスケジューラ)

`collector.py` → `predictor.py` → `site_generator.py` の順にまとめて実行する [run_all.bat](run_all.bat) を用意しています。

```
プログラム: D:\ClaudeCodeDir\news_summarizer\run_all.bat
開始場所: D:\ClaudeCodeDir\news_summarizer
```

上記をタスクスケジューラに登録すると、任意の間隔(例: 1日1回)でニュース収集・株価予測・サイト更新までを自動実行できます。

## Webサイトとして公開する(site_generator.py)

`output/` に蓄積されたJSONレポートから、閲覧しやすい静的HTMLサイトを生成できます。

```
python site_generator.py
```

`docs/` フォルダに以下が生成されます。

- `index.html` — 全キーワード横断の最新記事フィード(最大30件)
- `keywords/<キーワード>.html` — キーワードごとの記事アーカイブ(過去の収集分も累積して表示、同一URLは重複除去)
- `style.css` — スマートフォン対応のレスポンシブデザイン
- `sitemap.xml` / `robots.txt` — 検索エンジン向け

`collector.py` を実行してニュースを収集するたびに `site_generator.py` を実行すれば、サイトが最新のアーカイブに更新されます。

## 株価予測ログ(predictor.py、複数モデルの多数決)

収集したニュース要約をもとに、**複数のLLMモデル**でそれぞれ独立に「日本株式市場」「米国株式市場」への影響を明日・1週間後・1か月後の3期間について予測させ、モデル間の**多数決**で最終的なポジティブ/ネガティブを決定します。予測対象日を過ぎたものは、日経平均(`^N225`)・S&P500(`^GSPC`)の実際の値動きと自動的に比較され、的中(hit)/不的中(miss)が判定されます。

使用するモデルは [prediction_models.json](prediction_models.json) で管理します(同数決着を避けるため奇数個を推奨)。

```json
[
  "google/gemini-2.5-flash:free",
  "meta-llama/llama-3.3-70b-instruct:free",
  "nvidia/nemotron-3.5-lightning:free"
]
```

```
python collector.py   # ニュースを収集(まだの場合)
python predictor.py   # 各モデルでの予測生成 + 多数決 + 過去予測の答え合わせ
```

- モデルごとの予測は `predictions/by_model/<モデル名>.json` に**別ファイルで**蓄積されます(モデルごとに独立した的中率を追跡できます)
- 多数決による最終予測は `predictions/log.json` に蓄積されます
- どちらも追記形式で、過去の予測は消えません

`predictor.py` は実行するたびに、

1. モデルごとのログ・多数決ログの両方について、対象日を過ぎた未判定の予測を実際の指数データで答え合わせ
2. `output/` の各キーワードの最新ニュースをもとに、各モデルで新しい予測を生成(同じニュースセット×モデルに対する重複予測は作成しません)
3. 今回生成した予測をモデル間で多数決し、最終予測として記録(賛否同数の場合は「票同数(tie)」として記録し、的中判定の対象外とします)

を行います。定期実行(タスクスケジューラ)に組み込む場合は `collector.py` → `predictor.py` → `site_generator.py` の順に実行してください([run_all.bat](run_all.bat)にまとめてあります)。

### 予測ログを見る

```
python site_generator.py
```

を実行すると、以下が生成されます。

- `docs/predictions.html` — 多数決による最終予測の時系列一覧(各モデルの個別の理由・賛否の内訳つき)と的中率(総合・日本株・米国株別)
- `docs/models.html` — モデルごとの予測精度比較(どのモデルの的中率が高いか一覧できます)
- `docs/models/<モデル名>.html` — モデルごとの個別予測ログの時系列一覧

サイトの全ページのナビゲーションから「株価予測ログ」「モデル別精度」で確認できます。

**予測は情報提供のみを目的としたLLMによる自動生成コンテンツであり、投資助言ではありません。** 過去の的中率(多数決・各モデルとも)は将来の予測精度を保証するものではありません。

### デプロイ

`docs/` フォルダはそのまま静的ホスティングにアップロードできます。GitHub Pagesを使う場合は、リポジトリの Settings → Pages で「Deploy from a branch」を選び、公開ディレクトリを `/docs` に設定してください。実際に公開するURLが決まったら、sitemap.xml生成時のURLを合わせて更新します。

```
python site_generator.py --base-url https://your-domain.example.com
```

### 広告掲載(収益化)について

各ページのヘッダー直下とフッター直前に `<div class="ad-slot">` というプレースホルダーを用意しています。Google AdSense等の広告コードをこの中に貼り付けることで広告を掲載できます([site_generator.py](site_generator.py) の `AD_SLOT` 定数、または生成後の `docs/*.html` を直接編集)。

ただし、**このサイトは他サイトのニュースをLLMで要約して紹介する、いわゆる「まとめサイト」です**。Google AdSense等の広告審査は「独自性のある十分な価値のあるコンテンツ」を求めており、要約のみで構成される自動生成サイトは審査に通らない、または後日アカウント停止となるケースが少なくありません。収益化を目指す場合は、以下のような対応を検討してください。

- 要約に加えて独自の解説・分析・コメントを人力で追加する
- 各記事から原文サイトへのリンクを明確に保つ(本サイトは実装済み)
- 広告ネットワークの審査基準・著作権に関するポリシーを事前に確認する

広告枠の設置自体は用意していますが、審査通過や規約遵守については運営者ご自身の責任でご確認ください。
