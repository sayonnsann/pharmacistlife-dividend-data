# 古い配当性向の補完：実装・検証報告

対象は `feat/payout-estimate`。比較基準は `7bd30e08e04d519acb91a71e4068756416685832`。
main の作業フォルダには触れていない。本番反映、ワークフロー実行、checker-body 配置、push、コミットは行っていない。

## 変更

- 最終調整後の公式表示配当 `annual` と、画面用 `eps` を同じ決算期末年で接続し、DPS÷EPS×100を計算する。新たな分割調整や年度シフトは行わない。
- 総額との有効重複3年以上、丸め前の比の中央値0.8〜1.25、全比0.5〜2を境界込みで判定。総額0・推計正は不採用、両方0は重複数に含めない。
- 総額の最古年より前だけ補完する。EPS≦0・欠損・非4桁年キーを除外。推計1000%超は丸める前に除外し、採用値だけ小数第2位へ丸める。重複比較から総額1000%超を落とさない。
- 推計を実際に追加した会社にだけ `payoutRatioEstimated`、`payoutRatioDisplay`、`payoutRatioSource` を付ける。既存 `payoutRatioTotalBased`・`payoutRatio` と一覧の最新配当性向は変更しない。表示用総額は従来の0〜1000%範囲。
- `data/payout_estimate_holds.json` は38社。再試算で検出された年度矛盾25社、数値通過後の基準保留12社、EPS基準未確定の4825を保留する。表示基準・暫定分割・EPS調整根拠未確定等も実行時に除外する。保留ファイル欠落時はエラーにする。
- 両画面で表示用系列を優先し、推計を破線・白抜き点・算式のツールチップと年範囲の注記で区別する。財務欄の基準移行系列で上書きしない。推計年の業種中央値は描かない。
- 公開payloadの検証で、公式表示年、正のEPS、総額最古年より前、採用条件、表示値・算式ラベルの整合を再検査する。会社単位の許可を明示しない新推計は拒否する。

## 確認結果

| 確認 | 判定・実測 |
| --- | --- |
| 全テスト | 一致：`python3 -m pytest -q` → 478 passed, 3 skipped, 356 subtests passed |
| 既存テスト | 一致：追加後も通過。既存の3 skipは継続 |
| 推計追加 | 一致：3,790社中1,716社・5,242年分 |
| 新3フィールド | 一致：それぞれ1,716社の差分 |
| 既存payload全項目 | 一致：差分0、会社の追加・削除0 |
| 一覧・スクリーニング用全列 | 一致：差分0（最新配当性向を含む） |
| 再試算との全社・全採用年度・値照合 | 一致：差異0。5,242年すべて小数第2位で一致 |
| Decimalによる独立検算 | 一致：式、年度、EPS、1000%上限、既存総額優先の違反0 |
| 推計オフ | 一致：全3,790社の全stocks列とpayload文字列が比較基準と完全一致 |
| JS描画テスト | 一致：両グラフの値、推計点・線、欠測の切断、業種中央値の除外、基準移行の上書き防止、既存payloadの描画を検査 |
| 入力10ファイルのSHA-256 | 一致：読み取り前後で変更0 |
| `git diff --check` | 一致：エラー0 |

全件監査はローカルに予想入力がないため、`forecasts={}` として明示的に実施した。
予想入力付きの本番全件比較は未確認。既存の予想入力付きテストは通過している。
全社の原PDFによる株数基準の再監査は行っておらず、元報告書の保留と表示来歴契約を維持した。

実ブラウザでの目視確認は未確認。ローカルURLを開く操作はブラウザの自動承認チェックに拒否され、理由は「ユーザーがアクセス許可を拒否した」だった。迂回はしていない。ローカルHTTPサーバーは停止済み。

## 値の例（年度は決算期末年、値は%）

| コード・銘柄 | 追加した年と推計値 |
| --- | --- |
| 7466 SPK | 2013: 33.37、2014: 22.94、2015: 29.29 |
| 7203 トヨタ | 2013: 29.62、2014: 28.68、2015: 29.07、2016: 28.33、2017: 34.68、2018: 26.13、2019: 33.82 |

SPKの2013年は、画面用配当13.75円÷画面用EPS41.21円×100＝33.37%。
トヨタの2013年は18円÷60.76円×100＝29.62%。EPSの分割換算を推計関数で重ねていない。

## 元に戻す方法

`scripts/build_store.py` の `PAYOUT_ESTIMATE_ENABLED = False` に変更し、同じ入力でストアを再構築する。
新3フィールドが消え、画面は従来の系列に戻る。これは全3,790社で検証済み。本番再構築は今回実施していない。
注記・横軸文言・ツールチップも含めて画面自体を旧版へ完全復元する場合は、今回の `serving/checker.html` の差分も戻す。

## 検証成果物と再現

検証用SQLiteと非公開の入力・全件差分は `/private/tmp/pdd_payout_estimate_review/` にのみ保存した。本番投入用ではない。

- 最終オン比較：`final/on/payload_diff.json` と `final/on/numerator.json`
- 最終オフ比較：`final/off/payload_diff.json`
- 独立検算・全件試算照合・payload文字列一致：`verification.json`
- 入力ハッシュ：`input_hashes.json`、全テスト：`tests.log`
- ソースと一時成果物の全一覧：`created_files.json`（Python/pytestの自動キャッシュ・自動fixtureは別枠）

凍結した入力で再現するには、新しい出力ディレクトリを指定する：

```sh
python3 /private/tmp/pdd_payout_estimate_review/run_audits.py /private/tmp/pdd_payout_estimate_rerun
```

このスクリプトは `scripts/audit_store_diff.py` をオン・オフで実行する。オフの単独比較には同スクリプトの `--after-payout-estimates-off` を使える。

## 作成・変更ファイル（worktree内、未追跡を含む）

- `data/payout_estimate_holds.json`（新規）
- `scripts/payout_estimate.py`（新規）
- `scripts/build_store.py`
- `scripts/public_dividend_policy.py`
- `scripts/audit_store_diff.py`
- `serving/checker.html`
- `tests/test_payout_estimate.py`（新規）
- `tests/test_payout_estimate_ui.py`（新規）
- `tests/test_compare_yield_review_prs.py`（隔離fixtureへ新しい依存ファイルを追加）
- `docs/payout-estimate-2026-10-06.md`（新規、本報告書）

要判断：なし。未確認：予想入力付き本番全件比較、実ブラウザ目視、全社原PDFの分割基準監査。
