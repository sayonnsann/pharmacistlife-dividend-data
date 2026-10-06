"""古い決算期末年だけを公式表示DPS / 調整済みEPSで補完する。"""
from __future__ import annotations

import math
from statistics import median

FIELDS = ("payoutRatioEstimated", "payoutRatioDisplay", "payoutRatioSource")


def fiscal_numbers(raw: dict | None) -> dict[str, float]:
    return {str(y): v for y, v in (raw or {}).items()
            if len(str(y)) == 4 and str(y).isascii() and str(y).isdigit()
            and isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)}


def estimate_fields(payload: dict, display_years: set[int]) -> dict:
    """display_yearsは来歴検査済みの公式配当年。暦年・予想は渡さない。

    入力は完成したannual/epsのみ。株数の換算や年度の移動はここではしない。
    総額の描画上限は重複比較の後に適用し、既存系列は変更しない。
    """
    annual = fiscal_numbers(payload.get("annual"))
    eps = fiscal_numbers(payload.get("eps"))
    total = fiscal_numbers(payload.get("payoutRatioTotalBased"))
    if not total or any(int(y) not in display_years for y in annual):
        return {}
    estimates = {y: d / eps[y] * 100 for y, d in annual.items()
                 if d >= 0 and eps.get(y, 0) > 0}
    if any(t == 0 and estimates.get(y, 0) > 0 for y, t in total.items()):
        return {}
    ratios = [estimates[y] / t for y, t in total.items() if t > 0 and y in estimates]
    if (len(ratios) < 3 or not 0.8 <= median(ratios) <= 1.25
            or not all(0.5 <= r <= 2 for r in ratios)):
        return {}
    first = min(total)
    estimated = {y: round(v, 2) for y, v in sorted(estimates.items())
                 if y < first and 0 <= v <= 1000}
    if not estimated:
        return {}
    display = dict(estimated)
    display.update({y: v for y, v in total.items() if 0 <= v <= 1000})
    return {"payoutRatioEstimated": estimated, "payoutRatioDisplay": display,
            "payoutRatioSource": {y: "total_cash_paid" if y in total else "dps_eps_estimate"
                                  for y in display}}
