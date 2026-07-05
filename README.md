# 配当金推移チェッカー(pharmacistlife.site 導入用)

日本株全銘柄の配当金推移・連続増配年数・配当利回りを
検索できるページを作成するための一式です。参考サイトが更新停止していた反省を踏まえ、
**データ取得を自動化**し、手動更新に依存しない構成にしています。

## 構成

```
dividend-checker/
├── scripts/
│   ├── build_master.py   … JPX公式の上場銘柄一覧から銘柄マスタ(tickers.json)を作成
│   └── fetch_dividends.py … 各銘柄の配当履歴・株価をyfinanceで取得(dividends.json)
├── data/
│   ├── tickers.json       … 銘柄コード・銘柄名・市場区分(3,716銘柄)
│   └── dividends.json     … 配当実績データ(検証用に30銘柄のみ取得済み)
└── widget/
    └── dividend-checker.html … WordPressの「カスタムHTML」ブロックに貼り付ける本体
```

## データソースについて

| 項目 | 内容 |
|---|---|
| 銘柄マスタ | [JPX 東証上場銘柄一覧](https://www.jpx.co.jp/markets/statistics-equities/misc/01.html)(公式・無料・毎月更新) |
| 配当履歴 | [yfinance](https://github.com/ranaroussi/yfinance)(Yahoo Financeの非公式ラッパー、無料) |

J-Quants API(JPX公式)は配当情報の取得がPremiumプラン(月¥16,500)限定だったため、
無料で継続運用できるyfinanceを採用しました。トヨタ自動車で27年分、KDDIなどでも
20年超のデータが取得でき、直近(2026年)の実績も反映されることを確認済みです。

非公式ゆえに将来Yahoo側の仕様変更で壊れる可能性はありますが、コード側は
`fetch_dividends.py` の `fetch_one()` を差し替えるだけで他データソースに移行できる
構造にしてあります。

## セットアップ手順

### 1. 全銘柄データを取得する

```bash
cd scripts
pip3 install yfinance pandas openpyxl xlrd
python3 build_master.py        # 銘柄マスタを最新化(数秒)
python3 fetch_dividends.py     # 全銘柄の配当データを取得
```

- 全3,716銘柄を取得すると **Yahoo側への配慮のためのウェイトを含め1〜2時間程度**かかります。
- 処理は100銘柄ごとに `data/dividends.json` へ中間保存されるため、途中で止めても
  再実行すれば取得済み銘柄はスキップして続きから再開します。
- 動作確認だけしたい場合は `python3 fetch_dividends.py --limit 50` のように
  件数を絞れます。

### 2. データの置き場所(GitHub + jsDelivr)を用意する

「カスタムHTML貼り付け」方式では、`dividends.json` をどこかインターネット上に
置いて、そのURLをJavaScriptから読みに行く必要があります。無料で自動更新に
向いている **GitHub + jsDelivr CDN** を推奨します。

1. https://github.com/signup でGitHubアカウントを作成(未取得の場合)
2. 新規リポジトリを作成(例: `pharmacistlife-dividend-data`、Public設定)
3. このディレクトリの `data/` フォルダの中身をpush

```bash
cd /Users/yusuke/blog/sites/pharmacistlife/tools/dividend-checker
git init
git add data/tickers.json data/dividends.json
git commit -m "配当データ初回登録"
git branch -M main
git remote add origin https://github.com/<あなたのユーザー名>/pharmacistlife-dividend-data.git
git push -u origin main
```

4. jsDelivrのURLは以下の形式になります(pushの数分後から有効):

```
https://cdn.jsdelivr.net/gh/<ユーザー名>/pharmacistlife-dividend-data@main/data/dividends.json
```

### 3. ウィジェットのDATA_URLを書き換える

`widget/dividend-checker.html` 内の以下の行を、上記で確認したURLに置き換えます。

```js
var DATA_URL = "https://cdn.jsdelivr.net/gh/YOUR_GITHUB_USER/YOUR_REPO@main/data/dividends.json";
```

### 4. WordPressに貼り付ける

1. 固定ページを新規作成(例: 「配当金推移チェッカー」、パーマリンクは `dividend-checker` など)
2. ブロックエディタで「カスタムHTML」ブロックを追加
3. `widget/dividend-checker.html` の中身をすべてコピーして貼り付け
4. プレビューで検索・グラフ表示を確認してから公開

投資に関するコンテンツになるため、ページ末尾かフッターに
「本ページの情報は参考情報であり、投資判断は自己責任でお願いします。データの正確性は保証しません。」
といった免責文言を入れることを推奨します。

### 5. 月次更新を自動化する

参考サイトが更新停止した轍を踏まないための最重要ポイントです。以下のいずれかで
`fetch_dividends.py` を月1回実行し、GitHubへpushする運用にしてください。

- お使いのPCで `cron`(macOSなら`launchd`)に登録し、月初に自動実行+自動push
- この Claude Code 環境のスケジュールタスク機能で、月次実行を依頼する

更新すると `dividends.json` の内容が変わるため、`git add data/dividends.json && git commit && git push`
を実行すれば、jsDelivrのキャッシュ更新(通常1日以内)を経てページに反映されます。
キャッシュを即時反映したい場合は `@main` の代わりにコミットハッシュ指定のURLを使うか、
jsDelivrのパージAPIを利用してください。

## 今後の拡張候補

- 現状は先頭に検索したものだけでなくランキング表も表示(参考サイトの一覧機能を再現)
- ご自身の保有100銘柄だけを表示する「マイポートフォリオ」タブを追加
- 増配率の算出を「暦年」ではなく「決算期(会社ごとの配当基準日)」ベースに精緻化
