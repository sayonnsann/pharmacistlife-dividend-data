"""Fail closed on non-numeric cells in public annual financial series."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re

YEAR = re.compile(r"[0-9]{4}")
NUMERIC_SERIES = frozenset("""
roe roa equityRatio deRatio payoutRatioTotalBased doe eps bps per revenue netIncome
totalAssets operatingCashFlow investingCashFlow financingCashFlow
cashAndEquivalents interestBearingDebt dividendPerShare dividendYieldHistory
numberOfEmployees
""".split())
METRIC_CONTAINERS = ("basisTransitionMetrics", "basisSwitchMetrics")
# These are period-indexed evidence/labels, rather than numeric observations.
NON_NUMERIC_SERIES = frozenset({
    "basisTransition", "financialBasisByYear", "financialPeriods",
    "accountingStandards", "displayProvenance",
    "annualPending", "annualPartial", "dividendBreakdown",
})


def numeric_series_errors(record: dict) -> list[str]:
    """Inspect known metrics (even malformed ones) and new four-digit year maps.

    Do not infer the series type from its values: an entirely corrupt series
    containing only strings or objects must still fail validation.
    """
    errors = []
    code = record.get("code", "unknown")

    def inspect(container, prefix=""):
        for field, series in container.items():
            if field in NON_NUMERIC_SERIES or field in METRIC_CONTAINERS:
                continue
            year_map = isinstance(series, dict) and any(YEAR.fullmatch(str(y)) for y in series)
            if field not in NUMERIC_SERIES and not year_map:
                continue
            if not isinstance(series, dict):
                errors.append(f"code={code} field={prefix}{field}: annual series must be an object")
                continue
            for year, value in series.items():
                if not YEAR.fullmatch(str(year)):
                    continue
                valid = (value is None or type(value) is int
                         or (type(value) is float and math.isfinite(value)))
                if not valid:
                    errors.append(f"code={code} year={year} field={prefix}{field} value={value!r}: expected finite number or null")

    inspect(record)
    for section in METRIC_CONTAINERS:
        if section not in record:
            continue
        container = record[section]
        if not isinstance(container, dict):
            errors.append(f"code={code} field={section}: metric container must be an object")
        else:
            inspect(container, section + ".")
    return errors


def validate_financial_record(record: dict) -> None:
    errors = numeric_series_errors(record)
    if errors:
        raise ValueError("Invalid financial numeric series:\n" + "\n".join(errors))


def validate_financial_records(records: list[dict]) -> None:
    for record in records:
        validate_financial_record(record)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path,
                        default=[Path(__file__).resolve().parents[1] / "data/all_financials.json"])
    args = parser.parse_args(argv)
    failed = False
    for path in args.paths:
        with path.open(encoding="utf-8") as source:
            records = json.load(source)
        if not isinstance(records, list) or any(not isinstance(r, dict) for r in records):
            raise ValueError(f"{path}: expected array of companies")
        errors = [error for record in records for error in numeric_series_errors(record)]
        for error in errors:
            print(f"{path}: {error}")
        print(f"{path}: {len(records)} companies, {len(errors)} invalid cells/series")
        failed |= bool(errors)
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
