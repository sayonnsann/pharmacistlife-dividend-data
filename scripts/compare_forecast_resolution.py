#!/usr/bin/env python3
"""FTPへ書き込まず、同じ予想stateを旧版・新版で解決して要約する。"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import build_store as store


# -I の別プロセスで、指定された scripts/ とその版の補助モジュールだけを使う。
# 入出力はJSON。標準エラー・例外・入力の任意文字列を公開ログへ流さない。
BEFORE_WORKER = r"""
import contextlib, importlib.util, inspect, io, json, sys
from datetime import date
from pathlib import Path
try:
    path = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(path.parent))
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        spec = importlib.util.spec_from_file_location("build_store", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        data = json.load(sys.stdin)
        parameters = inspect.signature(module.forecast_on_price_basis).parameters
        result = {}
        for code, item in data["items"].items():
            record = module.apply_forecast_override(code, item["record"], data["overrides"])
            kwargs = {key: item[key] for key in
                      ("adjustment", "fiscal_month", "fiscal_record") if key in parameters}
            result[code] = module.forecast_on_price_basis(
                record, today=date.fromisoformat(data["today"]), **kwargs)
    print(json.dumps(result, allow_nan=False))
except Exception:
    sys.exit(1)
"""


def reference_annual(fiscal, adjustment, events, today):
    """build_store の公開用annualと同じ、調整済み実績だけを返す。"""
    if not fiscal or not fiscal.get("series"):
        return {}
    series = fiscal.get("streakSeries", fiscal["series"])
    unadjusted = store.adjustment_for_unadjusted_series(
        adjustment, series, fiscal_month=fiscal.get("fiscalMonth"),
        applied_actions=fiscal.get("appliedActions")) if adjustment else None
    annual = {
        str(year): (round(series[year] * store.adjustment_factor_for_period(
            unadjusted, year, fiscal_month=fiscal.get("fiscalMonth"), field="dividend"), 4)
            if unadjusted is not None else series[year])
        for year in fiscal["displaySeries"]
    }
    for year, basis_day in (fiscal.get("yuhoDisplayYears") or {}).items():
        day = date.fromisoformat(basis_day)
        if day > today:
            raise ValueError("future adoption")
        factor = 1.0
        seen = set()
        for event in events:
            old, new = store.finite_number(event.get("oldShares")), store.finite_number(event.get("newShares"))
            if (event.get("status") == "confirmed" and old and new and old > 0 and new > 0
                    and day < date.fromisoformat(event["effectiveDate"]) <= today):
                identity = (event["effectiveDate"], old / new)
                if identity not in seen:
                    factor *= old / new
                    seen.add(identity)
        annual[str(year)] = fiscal["displaySeries"][year] * factor
    return annual


def safe_basis(value):
    return value if isinstance(value, str) and re.fullmatch(r"[a-z_]{1,80}", value) else "-"


def number(value):
    value = store.finite_number(value)
    return "None" if value is None else format(value, ".10g")


def compare(state, before_module, fiscal_path, action_paths, overrides_path, today):
    if not state.is_file():
        raise ValueError("missing state")
    # ローダーの診断にはパス・出典URLが含まれ得るので、要約へは転送しない。
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        stocks = store.load_forecasts(state)
        fiscal = store.load_fiscal_dividends(fiscal_path)
        all_events = store.load_stock_actions(action_paths, as_of=date.max)
        overrides = store.load_forecast_overrides(overrides_path)
        items = {}
        resolved = {}
        warnings = []
        unresolved = []
        for code, raw in sorted(stocks.items()):
            if not re.fullmatch(r"[0-9A-Z]{4}", code) or not isinstance(raw, dict):
                continue
            record = store.apply_forecast_override(code, raw, overrides)
            if not any(record.get(key) is not None for key in (
                    "forecastDividend", "forecastDividendAdjusted",
                    "forecastInterimDividend", "forecastFinalDividend")):
                continue
            company_fiscal = fiscal.get(code)
            month = company_fiscal.get("fiscalMonth") if company_fiscal else None
            events = all_events.get(code, [])
            adjustment = store.split_adjustment(events)
            items[code] = dict(record=raw, adjustment=adjustment,
                               fiscal_month=month, fiscal_record=company_fiscal)
            after = store.forecast_on_price_basis(
                record, today=today, adjustment=adjustment, fiscal_month=month,
                fiscal_record=company_fiscal)
            resolved[code] = after
            if after["basis"] == "split_unresolved":
                unresolved.append(code)
            warnings.extend(f"{code}: {warning}" for warning in after.get("warnings", []))
            actual_events = [event for event in events
                             if date.fromisoformat(event["effectiveDate"]) <= today]
            annual = reference_annual(
                company_fiscal, store.split_adjustment(actual_events), actual_events, today)
            warning = store.forecast_ratio_warning(
                record, after["value"], annual, adjustment=adjustment,
                fiscal_month=month, fiscal_record=company_fiscal)
            if warning:
                warnings.append(f"{code}: {warning}")
    worker = subprocess.run(
        [sys.executable, "-I", "-B", "-c", BEFORE_WORKER, str(before_module.resolve())],
        input=json.dumps(dict(items=items, overrides=overrides, today=today.isoformat())),
        text=True, capture_output=True, check=False)
    if worker.returncode:
        raise ValueError("before resolution failed")
    before = json.loads(worker.stdout)
    lines = [
        f"予想分割補正の比較（today={today.isoformat()}）",
        f"予想を持つ会社: {len(items)}社",
        "",
        "| コード | 前の値 | 前のbasis | 後の値 | 後のbasis | 理由 |",
        "| --- | ---: | --- | ---: | --- | --- |",
    ]
    changed = 0
    for code, after in resolved.items():
        old = before[code]
        if (old["value"], old["basis"]) == (after["value"], after["basis"]):
            continue
        changed += 1
        basis = safe_basis(after["basis"])
        reason = ("分割補正を確定できず保留" if basis == "split_unresolved" else
                  "確定台帳で補完" if basis.endswith("_ledger") else
                  "年間欄の一致または内訳を優先")
        lines.append(
            f"| {code} | {number(old['value'])} | {safe_basis(old['basis'])} | "
            f"{number(after['value'])} | {basis} | {reason} |")
    lines.extend(["", f"変更: {changed}社", "",
                  f"予想分割保留: {len(unresolved)}社 / コード: "
                  + (", ".join(unresolved) or "なし"), "",
                  "予想分割警告（通知のみ）:"])
    if warnings:
        lines.extend("- " + warning for warning in warnings)
    else:
        lines.append("なし")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--before-module", type=Path, required=True)
    parser.add_argument("--fiscal-dividends", type=Path, default=store.DEFAULT_FISCAL_DIVIDENDS)
    parser.add_argument("--stock-actions", type=Path, default=store.DEFAULT_STOCK_ACTIONS)
    parser.add_argument("--stock-actions-extracted", type=Path, default=store.DEFAULT_EXTRACTED_STOCK_ACTIONS)
    parser.add_argument("--forecast-overrides", type=Path, default=store.DEFAULT_FORECAST_OVERRIDES)
    parser.add_argument("--today", help="YYYY-MM-DD（省略時は日本時間の実行日）")
    args = parser.parse_args()
    try:
        today = date.fromisoformat(args.today) if args.today else datetime.now(store.JST).date()
        report = compare(
            args.state, args.before_module, args.fiscal_dividends,
            [args.stock_actions, args.stock_actions_extracted], args.forecast_overrides, today)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as out:
                out.write(report + "\n")
    except Exception:
        # str(error)やtracebackは、秘密情報・ホスト・一時パスを含む可能性がある。
        parser.exit(1, "予想分割補正の比較に失敗しました（入力と比較元のscripts一式を確認してください）。\n")
    print(report, end="")


if __name__ == "__main__":
    main()
