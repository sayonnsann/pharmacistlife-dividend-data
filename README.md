# 配当金推移チェッカー(pharmacistlife.site 導入用)

日本株全銘柄の配当金推移・連続増配年数・配当利回りを
検索できるページを作成するための一式です。参考サイトが更新停止していた反省を踏まえ、
**データ取得を自動化**し、手動更新に依存しない構成にしています。

## 構成

```
dividend-checker/
├── scripts/
│   ├── build_master.py   … JPX公式の上場銘柄一覧から銘柄マスタ(tickers.json)を作成
│   ├── build_store.py     … 配当チェッカーが読むstocks.sqliteを作る
│   ├── fetch_forecasts.py … edinetdb.jpから配当予想・確定額を集める
│   ├── fetch_dividends.py … 【廃止予定】yfinanceで暦年の配当履歴を取得(dividends.json)
│   └── apply_haitoukin_fills.py … 【廃止予定】上の欠損年をhaitoukin-checker由来の値で補完
├── data/                 … このリポジトリに入るのはEDINET原本由来の2次資料と
│   │                       JPX由来の銘柄マスタだけ
│   ├── tickers.json       … 銘柄コード・銘柄名・市場区分(3,716銘柄)
│   ├── all_financials.json・sector_stats.json … EDINET由来の財務指標
│   └── (dividends.json / haitoukin_fills.json はここに置かない。後述)
├── edinet/{code}.json     … EDINET由来の決算月・EDINETコード
└── widget/
    └── dividend-checker.html … 旧・カスタムHTML版(現在は未使用。後述)
```

このリポジトリはjsDelivr配信のためPublicです。**公開するのはEDINET原本から作った
2次資料までで、それ以外の外部サイト由来のデータは置きません。** 置き場所と理由は
[非公開データの取り扱い方針](#非公開データの取り扱い方針)にまとめてあります。

## データソースについて

| 項目 | 内容 |
|---|---|
| 銘柄マスタ・名称・市場・業種 | [JPX 東証上場銘柄一覧](https://www.jpx.co.jp/markets/statistics-equities/misc/01.html)(公式・無料・毎月更新) |
| 配当実績（事業年度） | EDINETの有価証券報告書。古い年は haitoukin-checker で補完 |
| 財務指標・配当性向 | EDINETの有価証券報告書 |
| 配当予想・確定額 | [edinetdb.jp](https://edinetdb.jp/)（決算短信ベース、日次100件まで） |
| 株価・現在の年間配当 | kouhaitou-db の日次CSV（Yahoo由来） |

以前は配当履歴も [yfinance](https://github.com/ranaroussi/yfinance)（Yahoo Financeの
非公式ラッパー）から暦年ベースで取っていました。2026年8月に配当まわりをすべて
EDINET由来へ移し、**Yahooに残る役割は株価だけ**になりました。経緯と切り替え手順は
「[dividends.json（Yahoo由来の配当履歴）の廃止](#dividendsjsonyahoo由来の配当履歴の廃止)」を参照。

暦年をやめた理由は、3月決算の会社だと1つの暦年に「前期の期末配当＋当期の中間配当」が
入ってしまい、実在しない横ばいで連続増配が途切れるためです（KDDIが連続増配1年と
表示されていました）。

## セットアップ手順

### 1. 全銘柄データを取得する

> 2026年8月時点: 配当履歴はEDINET由来（`fiscal_dividends.json`）に移行済みで、
> ここで作る `dividends.json` はもう誰も読んでいません。新しく用意するときは
> `build_master.py` だけを動かしてください。以下は廃止手続きが終わるまでの記録です。

```bash
cd scripts
pip3 install yfinance pandas openpyxl xlrd
python3 build_master.py        # 銘柄マスタを最新化(数秒)
python3 fetch_dividends.py     # 【廃止予定】全銘柄の配当データを取得
```

- 全3,716銘柄を取得すると **Yahoo側への配慮のためのウェイトを含め1〜2時間程度**かかります。
- 処理は100銘柄ごとに `data/dividends.json` へ中間保存されるため、途中で止めても
  再実行すれば取得済み銘柄はスキップして続きから再開します。
- 動作確認だけしたい場合は `python3 fetch_dividends.py --limit 50` のように
  件数を絞れます。

### 2. 配当データの置き場所(ConoHaの非公開ディレクトリ)

`dividends.json` は **このリポジトリには置きません。** 中身はYahoo(yfinance)由来の
配当履歴に、haitoukin-checker由来の補完(`haitoukin_fills.json`)を重ねたものです。
haitoukin-checkerの運営者からは「利用してよい」との回答をもらっていますが、それは
**再配布の許諾ではありません。** このリポジトリはjsDelivr配信のためPublicなので、
置けば誰でもまとめてダウンロードできる形になってしまいます。

置き場所はConoHa WINGの非公開 `data` ディレクトリだけです。

- 作るのは週次ワークフロー(`.github/workflows/update-dividends.yml`)。
  毎週作り直したあと、FTPSでConoHaへ送ります(リポジトリへは入れません)。
- 使うのは日次ワークフロー(`.github/workflows/daily-store.yml`)。
  実行のたびにFTPSで取ってきて、GitHub Actionsの一時領域でSQLiteに入れます。
- 手で送るときは `edinet-direct/scripts/upload_private_data.sh <ファイル> dividends.json`

`haitoukin_fills.json`(440銘柄・1,660年分の補完値)も同じ理由でConoHaにだけ置きます。
静的なファイルなので、更新するときだけ手で送ります。

### 3. 表示側(現在の構成)

配当チェッカーの本番ページは、ConoHa上のPHP APIが `stocks.sqlite` から
必要な項目だけを返す構成です。データをまとめて配布する形にはなりません。

`widget/dividend-checker.html` は、jsDelivrの `dividends.json` を直接読んでいた
旧・カスタムHTML版です。配信をやめたので**このままでは動きません**。残してあるのは
経緯の記録用で、本番では使いません。

### 4. WordPressに貼り付ける(旧・カスタムHTML版の手順)

1. 固定ページを新規作成(例: 「配当金推移チェッカー」、パーマリンクは `dividend-checker` など)
2. ブロックエディタで「カスタムHTML」ブロックを追加
3. ウィジェットの中身をすべてコピーして貼り付け
4. プレビューで検索・グラフ表示を確認してから公開

投資に関するコンテンツになるため、ページ末尾かフッターに
「本ページの情報は参考情報であり、投資判断は自己責任でお願いします。データの正確性は保証しません。」
といった免責文言を入れることを推奨します。

### 5. 更新は自動

参考サイトが更新停止した轍を踏まないための最重要ポイントです。手作業は要りません。

- 週次(月曜早朝): `update-dividends.yml` が銘柄マスタと配当データを作り直します。
  `tickers.json` だけがリポジトリへcommit/pushされ、`dividends.json` はConoHaへ
  FTPSで送られます。
- 日次(07:00ごろ): `daily-store.yml` がConoHaから配当データを取ってきて
  `stocks.sqlite` を作り直し、ConoHaへ戻します。

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
公開データ                              非公開データ（リポジトリを通らない）
  ├─ このリポジトリ                       ConoHa WING ホームディレクトリ/data/
  │    ├─ data/all_financials.json         ├─ forecasts_state.json
  │    ├─ data/sector_stats.json           ├─ fiscal_dividends.json
  │    ├─ data/tickers.json                └─ calendar_dividends_frozen.json
  │    └─ edinet/{code}.json                   （14銘柄の凍結スナップショット）
  └─ kouhaitou-db（日次株価CSV）
                 │                                    │
                 └──────────────┬─────────────────────┘
                                ▼
                  GitHub Actions（毎日07:00ごろ）
                    1. ConoHaから予想state・事業年度の配当系列・
                       暦年の凍結スナップショットを取得
                    2. edinetdb.jpから最大95社の予想を取得
                    3. 3,808社分のstocks.sqliteを再構築
                    4. stateとSQLiteをFTPSでConoHaへ送信
                                │
                                ▼
                 ConoHa WING ホームディレクトリ/data/
                  ├─ forecasts_state.json（非公開・次回の待ち行列に使用）
                  └─ stocks.sqlite（PHP APIだけが読み取り）
```

週次（月曜早朝）の `update-dividends.yml` は、yfinanceで暦年の配当履歴を作り直して
ConoHaへ送るジョブです。2026年8月に配当系列をすべてEDINET由来へ移したため、
**このジョブの成果物（dividends.json）はもうどこからも読まれていません**。
止め方は「[dividends.json（Yahoo由来の配当履歴）の廃止](#dividendsjsonyahoo由来の配当履歴の廃止)」を参照。

### 事業年度の配当系列（リポジトリに置かないファイル）

配当の年別推移は「暦年」ではなく「事業年度」で並べています。暦年だと、3月決算の
会社では前期の期末配当と当期の中間配当が同じ年に入るため、実在しない横ばいが
生まれて連続増配が途切れます（KDDIが1年と表示されていたのはこれが原因）。

その事業年度ごとの系列が `data/fiscal_dividends.json` です。**このファイルは
リポジトリにコミットしません。** 年別セルの約半分が
[haitoukin-checker](https://haitoukin-checker.com/) から取り込んだ値で、運営者から
「利用してよい」との回答はもらっているものの、それは再配布の許諾ではないためです。
このリポジトリはjsDelivr配信のためPublicなので、置けば誰でもダウンロードできる形に
なってしまいます。

そのため、置き場所はConoHaの非公開 `data` ディレクトリだけにして、日次ワークフローが
実行のたびにFTPSで取ってきて、GitHub Actionsの一時領域でSQLiteに入れます。
生成物であるSQLiteはPHP API経由でしか読めないので、まとまったデータとして
配布される形にはなりません。

- 中身を作るのは `edinet-direct/scripts/build_fiscal_dividends.py`
- 連続増配を判定できない銘柄の印を付けるのは
  `edinet-direct/scripts/annotate_split_basis.py`
- ConoHaへ送るのは `edinet-direct/scripts/upload_fiscal_dividends.sh`
  （中身の確認をしてから、共通の `upload_private_data.sh` に渡します。
  FTPの4つの環境変数はGitHub Secretsと同じもの）
- `.gitignore` に予防的な規則を入れてあるので、うっかり `git add` しても入りません。
  さらに日次・週次どちらのワークフローも、先頭で追跡されていないことを確認して
  失敗させます。

手元で `scripts/build_store.py` を試すときは、
`edinet-direct/data/fiscal_dividends.json` を `data/` へコピーするか、
`--fiscal-dividends` でパスを指定してください。

### 連続増配年数を空欄にする銘柄

事業年度で並べると、EDINETの配当額が株式分割で揃っていない銘柄では「幻の減配」が
起きます。イエローハット(9882)は2025年度100円・2026年度62円と並びますが、
2025年4月1日に1→2分割しているので、62円は分割後の基準（実際は124円相当で増配）。
そのまま数えると16年続いた連続増配が0年になります。

こういう銘柄は **0年ではなく空欄** にします（不明なものを誤って表示しない方針）。

- `stocks.streak` / `streak_nd` は `NULL`。NULLは比較条件に一致しないので、
  「連続増配5年以上」のような絞り込みからは自動的に外れます
- `stocks.streak_unreliable` が `1`。データが無くてNULLなのか、
  基準が揃わず数えられないのかを画面側で区別できます
- `payload.streakUnreliable` に理由と該当年が入るので、
  「株式分割の影響で判定できません」と表示できます
- **配当系列そのものは残す**ので、年別配当のグラフはこれまで通り出ます
- 基準の切れ目をまたぐ平均増配率（cagr3 / cagr5 / cagr10）も同じ理由でNULLにします。
  またがない期間の増配率は残ります

対象銘柄は `edinet-direct/data/split_basis_suspects.json` に載っている分だけです。
自動検出（`annotate_split_basis.py --detect`）は候補を出すだけで、
そのままだと基準が揃っている普通の横ばい年まで拾ってしまうため、
確認した銘柄だけを一覧に載せる運用にしています。

SQLiteは、まず `stocks.sqlite.new` という一時名で全体をアップロードし、アップロード
完了後にFTPの名前変更命令で `stocks.sqlite` と差し替えます。PHPが読み取り中でも、
途中までしか届いていないファイルを開くことはありません。予想stateも同様に一時名を
経由します。

edinetdb.jpの無料枠は1日100リクエストなので、既定では95社だけ取得して5件分を残します。
決算発表を跨いでまだ取得していない会社を先にし、その中では優先銘柄、配当利回りの高い
順に処理します。それ以外の会社も待ち行列を巡回するため、初回は約34日で一巡します。

### 増配・分割に早く気づく（イベント枠）

決算月から作る発表日は「決算月の2か月後の15日」という近似で、実際の発表とは最大2週間
ずれます。東計電算(4746)は2026年8月3日のQ2決算短信で株式分割と大幅増配を出しましたが、
近似日は8月15日なので、巡回だけだと12日間気づけませんでした。

そこで日次更新の最初に、edinetdb.jpの `/v1/events` で直近の開示を見て、
**1日の枠のうち最大20社ぶんをイベント枠**に割り当てます。残りはこれまでどおりの
巡回です。イベント枠が埋まらない日は、余りは巡回に回るので枠は遊びません。

| 見ている開示 | 1日の件数 | 扱い |
|---|---|---|
| `dividend_revision`（配当予想の修正） | 平常3〜10件・繁忙期31件 | 全部見る。最優先 |
| `stock_split` / `reverse_split`（分割・併合） | 年168件 | 全部見る。次点 |
| `earnings_summary`（決算短信） | 平常92件・繁忙期780件 | 上限200件で打ち切り。最後 |

決算短信は繁忙期に全部は追えないので、打ち切った件数を実行ログに出します
（`events ... truncated=` の行）。黙って切り捨てはしません。

イベント取得そのものも1日100件の枠を使う（1日5〜6リクエスト）ため、その分だけ
予想取得の上限を自動的に下げます。どこまで見たか・どの開示を処理したかは
`forecasts_state.json` の `events` に記録され、失敗した日は翌日に取り直します。

止めたいときは、`daily-store.yml` の `DVC_EVENT_SLOTS` を `0` にすると
イベントAPIを一切呼ばず、従来どおりの巡回だけになります。

### 特定の銘柄を今すぐ取り直す

イベント枠でも拾えなかった、あるいは急いで直したいときは、GitHubの `Actions` から
**「指定銘柄の配当予想を今すぐ取り直す（手動実行）」**を実行します。銘柄コードを
カンマ区切りで入れるだけです（例: `4746,9433`）。EDINETコードと決算月は
`edinet/{code}.json` から自動で引きます。

更新されるのは `forecasts_state.json` だけなので、画面に出るのは次の日次更新のあとです。
すぐ反映したい場合は、続けて「配当チェッカーDBの日次更新」を手動実行してください。

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

   なお、次の3つは**事前にConoHaへ置いておく必要があります**。無いとワークフローは
   取得ステップで止まります。

   | 置くファイル | 送り方 | 無いと止まるステップ |
   |---|---|---|
   | `fiscal_dividends.json` | `upload_fiscal_dividends.sh` | 日次「事業年度の配当系列をFTPSから取得」 |
   | `dividends.json` | `upload_private_data.sh <ファイル> dividends.json` | 日次「暦年の配当履歴をFTPSから取得」 |
   | `haitoukin_fills.json` | `upload_private_data.sh <ファイル> haitoukin_fills.json` | 週次「haitoukin補完データをFTPSから取得」 |

   2回目以降、`dividends.json` は週次ワークフローが自動で更新するので、
   手で送るのは初回だけです。
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
しなければ予想列は `NULL` のまま構築されます。事業年度の配当系列と凍結スナップ
ショットはリポジトリに入っていないので、手元のファイルのパスを指定します
（ConoHaから落としたもの、または `edinet-direct/data/` のもの）。

```bash
python3 scripts/build_store.py \
  --fiscal-dividends ../edinet-direct/data/fiscal_dividends.json \
  --calendar-dividends ../edinet-direct/data/calendar_dividends_frozen.json \
  --output /tmp/stocks.sqlite
sqlite3 /tmp/stocks.sqlite \
  "SELECT count(*) FROM stocks; SELECT code,price,forecast_yield FROM stocks WHERE code='8058';"
# 連続増配を判定できない銘柄の確認
sqlite3 /tmp/stocks.sqlite \
  "SELECT count(*) FROM stocks WHERE streak_unreliable=1; SELECT code,name,streak FROM stocks WHERE code='9882';"
```

### 非公開データの取り扱い方針

外部から取得した値そのものを含むファイルは、このリポジトリに置きません。
このリポジトリはjsDelivr配信のためPublicで、置けば誰でもダウンロードできるためです。

公開してよいのは **EDINET原本から作った2次資料まで** です。それ以外の外部由来の値を
含むファイルは、ConoHaの非公開 `data` ディレクトリにだけ置き、表示はPHP API経由に
限ります。

| ファイル | 出どころ | なぜ公開しないか | 置き場所 |
|---|---|---|---|
| `forecasts_state.json` | edinetdb.jpの配当予想 | 取得先のデータをそのまま含むため | ConoHaの非公開 `data` のみ |
| `fiscal_dividends.json` | EDINET＋haitoukin-checker | 年別セルの約半分がhaitoukin-checker由来。許諾は「利用してよい」であって再配布の許諾ではない | ConoHaの非公開 `data` のみ |
| `calendar_dividends_frozen.json` | Yahoo(yfinance)の凍結分（14銘柄） | Yahoo由来の配当履歴そのもの。更新はしないが再配布はできない | ConoHaの非公開 `data` のみ |
| `dividends.json` | Yahoo(yfinance)＋haitoukin-checker | Yahoo由来の配当履歴の再配布にあたるうえ、上の補完値も混ざっている。**2026年8月に読み手がゼロになった（廃止手続き中）** | ConoHaの非公開 `data` のみ |
| `haitoukin_fills.json` | haitoukin-checker | haitoukin-checker由来の配当額そのもの。許諾は利用のみ | ConoHaの非公開 `data` のみ |
| `stocks.sqlite` | 上記の生成物 | 上記を含むため | ConoHaの非公開 `data` のみ（PHP API経由で参照） |

- いずれも `.gitignore` の対象で、リポジトリへcommit/pushする処理はありません。
- 日次・週次どちらのワークフローも先頭で `fiscal_dividends.json` /
  `dividends.json` / `haitoukin_fills.json` が追跡されていないことを確認し、
  入っていたら失敗させます。
- Actions Artifactへアップロードする処理もありません。
- ワークフローのログにはAPIキー、FTPパスワード、予想値を出力しません。
- `data/all_financials.json`、`data/sector_stats.json`、`data/tickers.json`、
  `edinet/` は、EDINETとJPXという再配布できる出どころの静的データであり、
  上の5つとは別物です。

`dividends.json` は2026年8月まで、このリポジトリの `data/` に入った状態で
jsDelivrから配信されていました。追跡から外して以降は
`https://cdn.jsdelivr.net/gh/sayonnsann/pharmacistlife-dividend-data@main/data/dividends.json`
が404になります（キャッシュが残るあいだは古い内容が返ることがあります）。
このURLを読んでいた手元の試作品は、ローカルのファイルを読むように切り替えました。

## 株式分割イベントの月次反映

配信側の補正データは、別のEDINETデータ基盤にある
`data/stock_action_ledger.json` を原本にして毎月再生成されます。正式cloneで
`scripts/filter_extracted_stock_actions.py` を実行し、次の条件を満たす株式分割だけを
`data/stock_actions_extracted.json` と比較します。

- 効力発生日が配当系列の最終年度末より後
- 比率が50倍未満
- `data/tickers.json` に存在するJPX上場銘柄
- `action=split` かつ、台帳の `duplicateOf` がない

差分がある場合だけ `auto/stock-actions-YYYYMM` ブランチからPRが作られ、本文に追加・削除イベントの
銘柄コード、効力発生日、比率が列挙されます。PR作成後は `gh pr merge --auto --squash` が予約されます。
差分がない月はPRを作りません。

### 自動マージの安全弁

`.github/workflows/validate-stock-actions.yml` は、PRの全テストを実行したうえで、変更ファイルが
`data/stock_actions_extracted.json` だけであることを検査します。また、mainとの差分でイベント追加が51件以上、
または削除が11件以上なら `::error::` で失敗します。条件を外れたPRは自動マージされず、通常のレビュー待ちとして残ります。

自動マージとrequired checkの有効化はリポジトリ設定を変更するため、内容を確認してから実行してください。

```bash
gh api --method PATCH repos/sayonnsann/pharmacistlife-dividend-data \
  -F allow_auto_merge=true

gh api --method PUT repos/sayonnsann/pharmacistlife-dividend-data/branches/main/protection \
  -H 'Accept: application/vnd.github+json' \
  --input - <<'JSON'
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["validate"]
  },
  "enforce_admins": false,
  "required_pull_request_reviews": null,
  "restrictions": null
}
JSON
```

workflowやfilterの初回導入PRはデータ1ファイルだけという自動更新PRの制約に該当しないため、通常のレビューで先に取り込んでください。
以後の自動更新PRは、データファイル以外を混ぜない限りこの検証を通ります。

### launchdジョブの停止

月次ジョブ自体はデータ生成側のMacでlaunchdが起動します。停止する場合は、そのMacで次を実行します。

```bash
launchctl bootout "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/com.sayonnsann.stock-actions-monthly.plist"
launchctl disable "gui/$(id -u)/com.sayonnsann.stock-actions-monthly"
```

ログは `~/Library/Logs/edinet-direct-stock-actions.log` と同じディレクトリのlaunchd出力に残ります。

## dividends.json（Yahoo由来の配当履歴）の廃止

`data/dividends.json` は、yfinance（Yahoo Finance）から全銘柄の暦年ベースの配当履歴を
取ってきたファイルです。2026年8月の作業で、このファイルが担っていた役割をすべて
別のデータに移しました。**現時点でこのファイルを読むコードはありません。**

### 何をどこへ移したか

| もとの役割 | 移した先 | 備考 |
|---|---|---|
| 配当実績の棒グラフ | `fiscal_dividends.json`（EDINET＋haitoukin-checker） | 2026年8月より前に移行済み。3,337銘柄 |
| 今年の「集計中」バー | edinetdb.jp の予想・確定額（`forecasts_state.json`） | 権利落ちベースの途中累計 → 会社発表の「予想」「確定」に置き換え |
| 配当性向の折れ線 | `all_financials.json` の `payoutRatioTotalBased`（EDINET） | 暦年→事業年度になり棒グラフと軸が揃う。年数も中央値3年→9年 |
| 事業年度の系列が無い14銘柄の棒グラフ | `calendar_dividends_frozen.json`（凍結スナップショット） | 東京電力(9501)など。更新しない |
| 銘柄名・市場・業種 | `data/tickers.json`（JPX由来・このリポジトリ内） | 3,716銘柄で値は完全一致 |
| 予想取得の対象銘柄・利回り順 | `fiscal_dividends.json` ＋ kouhaitou-db の日次CSV | `fetch_forecasts.py` |

### 止める手順

前提として、この変更を含むコミットが `main` に入り、日次ワークフローが1回
成功していること（`stocks.sqlite` の meta に `calendar_dividends_source` が
入っていれば新しい構成で動いています）。

1. **凍結スナップショットをConoHaへ置く（先にやる）**
   `edinet-direct/data/calendar_dividends_frozen.json` を、FTPSで
   ConoHaの非公開 `data` ディレクトリへ送ります。これを置く前に
   `dividends.json` を消すと、14銘柄の配当グラフが空になります。

2. **週次ワークフローを止める**
   `.github/workflows/update-dividends.yml` の `on:` から `schedule:` を外します
   （`workflow_dispatch: {}` だけ残す）。毎週1〜3時間かかっていたジョブが止まります。
   > 注意: このワークフローは `data/tickers.json`（銘柄マスタ）の更新も担っています。
   > tickers.json は今も使っているので、`build_master.py` だけを動かす軽い
   > ワークフローに作り替えるか、月1回の手動実行に切り替えてください。
   > **schedule を外すだけだと銘柄マスタが更新されなくなります。**

3. **1〜2週間ようすを見る**
   日次ワークフローが毎朝成功し、配当チェッカーの表示（棒グラフ・配当性向の
   折れ線・予想バー）に異常が出ないことを確認します。この間 `dividends.json` は
   ConoHaに置いたままにします（4に戻れるのはこの期間だけです）。

4. **ConoHa上の dividends.json を消す**
   3で問題が出なければ、ConoHaの非公開 `data` ディレクトリから
   `dividends.json` と `dividends.json.bak` を削除します。同時に
   `haitoukin_fills.json` も、`fiscal_dividends.json` を作り直すとき以外は
   使わなくなっています（消さずに残しておいても害はありません）。

5. **不要になったスクリプトを消す**
   `scripts/fetch_dividends.py` と `scripts/apply_haitoukin_fills.py` は
   `dividends.json` を作るためのものです。4まで終えてから消します。
   `scripts/enrich_payout_ratio.py` も暦年の配当性向を足すためのもので不要です。

### 元に戻すとき（ロールバック）

- **手順3までの間**: `git revert` でこの変更を戻し、日次ワークフローを手動実行
  すれば元の構成に戻ります。ConoHaの `dividends.json` はそのまま残っています。
- **手順4のあと**: `update-dividends.yml` を手動実行すれば、1〜3時間で
  `dividends.json` が作り直されてConoHaに置かれます。`haitoukin_fills.json` が
  ConoHaに残っていることが条件です。
- **どの時点でも**: 直前のSQLiteは `stocks.sqlite.bak` としてConoHaに残るので、
  表示だけを急いで戻したいときは `.bak` を `stocks.sqlite` にリネームします。

### この移行で悪くなったところ

移行前後で全3,808銘柄を比較した結果です。良くなった点だけでなく、失ったものも
残しておきます。

| 変化 | 銘柄数 | 中身 |
|---|---|---|
| 配当性向の折れ線が消えた | 293 | EDINETが配当性向を出せない銘柄。Yahooの暦年値には戻さない |
| 配当性向の折れ線が出た | 182 | 逆にEDINETにだけある銘柄 |
| 配当性向の絞り込みから外れた | 292 | `payout` 列がNULLになり、「配当性向◯%以下」で出てこなくなる |
| 連続増配年数が 0年 → 「-」 | 441 | 配当履歴そのものが無い銘柄。絞り込み結果は変わらない（0もNULLも `>= 1` に一致しない） |
| 「集計中」バーが消えた | 8 | 事業年度の系列が無い銘柄。edinetdb.jpが予想を返せば「予想」バーとして戻る |
| 株価が出なくなった | 3 | 7317 松屋アールアンドディ / 3681 ブイキューブ / 7940 ウェーブロックホールディングス。kouhaitou-dbに無く、Yahooの株価で埋めていた |
| 10年平均増配率が消えた | 1 | 5981 東京製綱。年が飛んでいて「10年前の年」が系列に無いため |

また、「予想」バーはedinetdb.jpのAPI（日次100件）で1銘柄ずつ集めているため、
**全3,808銘柄に行き渡るまで40日ほどかかります**。それまでは取得済みの銘柄にだけ
バーが出ます。以前の「集計中」バーは全銘柄に出ていたので、移行直後は
バーのある銘柄が一時的に減ります。
