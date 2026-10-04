#!/usr/bin/env python3
"""利回り分子の旧/新計算を全CSV銘柄で比較する（生成データは変更しない）。"""
from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from datetime import date, datetime
from pathlib import Path

import build_store as store


def audit(prices_url, fiscal_path, action_paths, edinet_dir, *, today,
          split_prices_url=None, baseline_action_paths=None,
          overrides_path=store.DEFAULT_YIELD_NUMERATOR_OVERRIDES):
    prices, dividends, updated = store.load_daily_prices(prices_url)
    fiscal = store.load_fiscal_dividends(fiscal_path)
    actions = store.load_stock_actions(action_paths, as_of=today)
    baseline_actions = (store.load_stock_actions(baseline_action_paths, as_of=today)
                        if baseline_action_paths is not None else actions)
    active = store.load_yield_split_adjustments(split_prices_url or prices_url)
    overrides = store.load_yield_numerator_overrides(overrides_path)
    rows = []
    for code in sorted(prices):
        events = actions.get(code, [])
        adjustment = store.split_adjustment(events)
        record = fiscal.get(code, {})
        month = record.get("fiscalMonth")
        year = store.load_yield_source_year(code, edinet_dir)
        baseline = store.split_adjustment(baseline_actions.get(code, []))
        old_factor = baseline["dividendFactor"] if baseline else 1.0
        new_factor = store.yield_numerator_factor(
            code, events, adjustment, source_year=year, fiscal_month=month,
            active_adjustments=active, today=today, override_event_ids=overrides,
        )
        raw = dividends[code]
        series = record.get("series", {})
        reference = None
        if series:
            if adjustment:
                pending = store.adjustment_for_unadjusted_series(
                    adjustment, series, fiscal_month=month,
                    applied_actions=record.get("appliedActions"),
                )
                series = {
                    year: round(value * store.adjustment_factor_for_period(
                        pending, year, fiscal_month=month, field="dividend",
                    ), 4)
                    for year, value in series.items()
                }
            reference = store.finite_number(series[max(series, key=int)])
        before, after = raw * old_factor, raw * new_factor
        comparable = reference is not None and reference > 0
        rows.append(dict(
            code=code, raw=raw, price=prices[code], source_year=year,
            fiscal_month=month, before=before, after=after, reference=reference,
            before_yield=round(before / prices[code] * 100, 2),
            after_yield=round(after / prices[code] * 100, 2),
            before_ratio=before / reference if comparable else None,
            after_ratio=after / reference if comparable else None,
            before_bad=comparable and not 0.6 <= before / reference <= 1.6,
            after_bad=comparable and not 0.6 <= after / reference <= 1.6,
            comparison_status="compared" if comparable else "no_positive_reference",
        ))
    before_bad = [r["code"] for r in rows if r["before_bad"]]
    after_bad = [r["code"] for r in rows if r["after_bad"]]
    return dict(
        as_of=today.isoformat(), prices_updated=updated,
        split_state_available=active is not None, total=len(rows),
        compared=sum(r["comparison_status"] == "compared" for r in rows),
        before_bad=before_bad, after_bad=after_bad,
        newly_bad=sorted(set(after_bad) - set(before_bad)), rows=rows,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prices", type=Path, required=True)
    parser.add_argument("--split-adjustments", type=Path, required=True)
    parser.add_argument("--fiscal-dividends", type=Path, required=True)
    parser.add_argument("--edinet-dir", type=Path, default=store.REPOSITORY_ROOT / "edinet")
    parser.add_argument("--stock-actions", type=Path, default=store.DEFAULT_STOCK_ACTIONS)
    parser.add_argument("--stock-actions-extracted", type=Path,
                        default=store.DEFAULT_EXTRACTED_STOCK_ACTIONS)
    parser.add_argument("--baseline-stock-actions", type=Path,
                        help="変更前のmanual台帳。省略時は比較対象と同じ台帳")
    parser.add_argument("--yield-numerator-overrides", type=Path,
                        default=store.DEFAULT_YIELD_NUMERATOR_OVERRIDES)
    parser.add_argument("--as-of", type=date.fromisoformat, default=datetime.now(store.JST).date())
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    # ローカルの任意ファイル名も本番と同じ取得・検証関数を通す。
    with tempfile.TemporaryDirectory() as directory:
        split_file = Path(directory) / "split_adjustments.json"
        if args.split_adjustments.exists():
            shutil.copyfile(args.split_adjustments, split_file)
        result = audit(
            args.prices.resolve().as_uri(), args.fiscal_dividends,
            [args.stock_actions, args.stock_actions_extracted], args.edinet_dir,
            today=args.as_of, overrides_path=args.yield_numerator_overrides,
            split_prices_url=(Path(directory) / "database.csv").as_uri(),
            baseline_action_paths=([args.baseline_stock_actions, args.stock_actions_extracted]
                                   if args.baseline_stock_actions else None),
        )
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    for key in ("total", "compared", "before_bad", "after_bad", "newly_bad"):
        value = result[key]
        print(f"{key}: {len(value) if isinstance(value, list) else value}"
              + (f" {','.join(value)}" if isinstance(value, list) else ""))


if __name__ == "__main__":
    main()
