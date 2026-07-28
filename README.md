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

## サーバー版DBの日次自動更新

`.github/workflows/daily-store.yml` は毎日07:00ごろ（日本時間）に、サーバー版
配当チェッカーが読む `stocks.sqlite` を作り直します。GitHub Actions側の混雑で、
開始時刻が数分から数十分遅れることがあります。

### 全体の流れ

```text
公開データ
  ├─ このリポジトリ
  │    ├─ data/all_financials.json・sector_stats.json
  │    ├─ data/dividends.json
  │    └─ edinet/{code}.json（決算月・EDINETコード）
  └─ kouhaitou-db（日次株価CSV）
                 │
                 ▼
        GitHub Actions（毎日07:00ごろ）
          1. ConoHaから非公開の予想stateを取得
          2. edinetdb.jpから最大95社の予想を取得
          3. 3,808社分のstocks.sqliteを再構築
          4. stateとSQLiteをFTPSでConoHaへ送信
                 │
                 ▼
 ConoHa WING ホームディレクトリ/data/
  ├─ forecasts_state.json（非公開・次回の待ち行列に使用）
  └─ stocks.sqlite（PHP APIだけが読み取り）
```

SQLiteは、まず `stocks.sqlite.new` という一時名で全体をアップロードし、アップロード
完了後にFTPの名前変更命令で `stocks.sqlite` と差し替えます。PHPが読み取り中でも、
途中までしか届いていないファイルを開くことはありません。予想stateも同様に一時名を
経由します。

edinetdb.jpの無料枠は1日100リクエストなので、既定では95社だけ取得して5件分を残します。
決算発表を跨いでまだ取得していない会社を先にし、その中では優先銘柄、配当利回りの高い
順に処理します。それ以外の会社も待ち行列を巡回するため、初回は約34日で一巡します。

### GitHub Secretsの設定

リポジトリのGitHub画面で `Settings` → `Secrets and variables` → `Actions` →
`New repository secret` を開き、次の名前で1件ずつ登録します。値はワークフローや
READMEへ直接書かないでください。

| Secret名 | 入れる値 | 例・注意 |
|---|---|---|
| `FTP_HOST` | ConoHaのFTPサーバー名 | `ftp://`や末尾のパスを付けず、ホスト名だけ |
| `FTP_USER` | ConoHaのFTPユーザー名 | 日次更新に使うFTPアカウント |
| `FTP_PASS` | 上記アカウントのパスワード | GitHubにSecretとしてのみ保存 |
| `FTP_REMOTE_DIR` | FTP接続後に見える保存先 | 通常は `/data`。末尾の `/` はあってもなくても可 |
| `EDINETDB_API_KEY` | edinetdb.jpで発行したAPIキー | ログには出力されない |
| `PRIORITY_CODES` | 先に更新したい銘柄コード | 任意。例: `8058,7203,9433` |

ConoHa WINGでFTP情報を確認する場所は次のとおりです。

1. ConoHaコントロールパネルへログインし、上部の `WING` を選びます。
2. FTPサーバー名は `サーバー管理` → `契約情報` →
   `メール/FTP/ネームサーバー情報` の `FTPサーバー` で確認します。
3. FTPユーザー名は `サイト管理` → `FTP` で確認します。対象ユーザーを開くと、
   パスワードの変更と接続許可ディレクトリの確認もできます。
4. FTPソフトなどで一度接続し、ホームディレクトリ直下の `data` が
   `/data` として見えることを確認します。FTPアカウントの接続許可ディレクトリを
   `data` 自体にした場合は、接続後のルートが保存先になるため
   `FTP_REMOTE_DIR` は `/` にします。

画面の場所は
[ConoHa WING公式「FTPソフトを設定する」](https://support.conoha.jp/w/ftpclient/)
でも確認できます。FTPアカウントには、可能なら日次更新に必要なディレクトリだけを
許可してください。

### 初回の手動実行

Secretsを保存したら、最初は自動実行を待たずに確認します。

1. GitHubで `Actions` → `配当チェッカーDBの日次更新` を開きます。
2. `Run workflow` → `Run workflow` を押します。
3. 全ステップが緑色になり、最後に
   `配当チェッカーDBの日次更新が完了しました。` と出ることを確認します。
4. ConoHaの `data` ディレクトリに `forecasts_state.json` と
   `stocks.sqlite` が作られたことを確認します。
5. 配当チェッカーで8058などを検索し、価格が直近営業日の値になっていることを
   確認します。

初回はサーバーに予想stateがなくても正常です。空の待ち行列から開始します。
途中で失敗した場合は成功扱いにならず、Actions画面の該当ステップが赤くなります。
認証失敗ならFTPの3項目、ファイルが見つからない場合は `FTP_REMOTE_DIR` と接続許可
ディレクトリを確認してください。

### ローカルでの確認

APIキーなしで待ち行列だけ確認できます。このモードはAPIを呼ばず、stateも作成・変更
しません。日付を固定すると決算期の優先順位も再現できます。

```bash
python3 scripts/fetch_forecasts.py \
  --dry-run \
  --today 2026-05-15 \
  --print-limit 20
```

SQLiteの構築確認には日次株価CSVへのインターネット接続が必要です。予想stateを指定
しなければ予想列は `NULL` のまま構築されます。

```bash
python3 scripts/build_store.py --output /tmp/stocks.sqlite
sqlite3 /tmp/stocks.sqlite \
  "SELECT count(*) FROM stocks; SELECT code,price,forecast_yield FROM stocks WHERE code='8058';"
```

### edinetdb由来データの非公開保存方針

edinetdb.jpから取得した予想値を含むファイルそのものは公開・再配布しません。
`forecasts_state.json` と `stocks.sqlite` は、GitHub Actionsの一時ディレクトリと
ConoHa WINGの非公開 `data` ディレクトリにだけ置かれます。配当チェッカーは
ConoHa上のPHP APIを通して表示に必要な項目だけを返します。

- どちらも `.gitignore` の対象で、リポジトリへcommit/pushする処理はありません。
- Actions Artifactへアップロードする処理もありません。
- ワークフローのログにはAPIキー、FTPパスワード、予想値を出力しません。
- `data/all_financials.json`、`data/sector_stats.json`、`edinet/` はEDINET由来の
  再配布可能な静的データであり、edinetdb.jpの予想stateとは別物です。
