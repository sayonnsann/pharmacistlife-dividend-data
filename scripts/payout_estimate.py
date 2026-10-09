"""古い決算期末年だけを公式表示DPS / 調整済みEPSで補完する。"""
from __future__ import annotations

import math
from statistics import median

FIELDS = ("payoutRatioEstimated", "payoutRatioDisplay", "payoutRatioSource")


def inclusive_range(value: float, lower: float, upper: float) -> bool:
    # 閾値ちょうどの除算誤差だけを吸収する。表示丸めで判定しない。
    return (lower <= value <= upper or math.isclose(value, lower, rel_tol=1e-12)
            or math.isclose(value, upper, rel_tol=1e-12))


def fiscal_numbers(raw: dict | None) -> dict[str, float]:
    return {str(y): v for y, v in (raw or {}).items()
            if len(str(y)) == 4 and str(y).isascii() and str(y).isdigit()
            and isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)}


def boundary_issue(estimates: dict[str, float], total: dict[str, float]) -> dict | None:
    """総額最古年の直前年が比較できなければ、古い年は採用しない。"""
    first = min(total)
    previous = str(int(first) - 1)
    if previous not in estimates or total[first] <= 0:
        return {"reason": "boundary_unavailable", "years": [previous, first]}
    ratio = estimates[previous] / total[first]
    if not 0.5 <= ratio <= 2:
        return {"reason": "boundary_ratio", "years": [previous, first], "ratio": ratio}
    return None


def eps_step_events(payload: dict, start: str, end: str) -> list[dict]:
    """連続する決算期末年のみ比較。整数倍率のEPS変動を株数基準の疑いとして記録。

    純利益もほぼ同じ倍率で動いた場合は自然な増減益として残す。前年比の
    比0.8〜1.25は通常の株数変化も許容し、整数倍の基準ずれと区別する。
    総額の欠損・符号不一致を利益変動と解釈せず、EPSだけによる疑いを維持する。
    配当欠損や描画上限で落ちた年も、推計期間内のEPS段差検査には含める。
    """
    eps = fiscal_numbers(payload.get("eps"))
    income = fiscal_numbers(payload.get("netIncome"))
    years = sorted(y for y in eps if start <= y <= end)
    events = []
    for older, newer in zip(years, years[1:]):
        if int(newer) - int(older) != 1 or eps[older] * eps[newer] <= 0:
            continue
        eps_ratio = eps[newer] / eps[older]
        multiple = max(eps_ratio, 1 / eps_ratio)
        if not math.isfinite(multiple):
            continue
        integer = round(multiple)
        if (multiple < 1.8 or integer < 2
                or not inclusive_range(multiple, .97 * integer, 1.03 * integer)):
            continue
        event = {"reason": "eps_integer_step", "years": [older, newer],
                 "epsRatio": eps_ratio, "multiple": multiple, "integerMultiple": integer}
        # 正負の向きも一致する純利益のみ。符号反転は比較しない。
        if (income.get(older, 0) * eps[older] > 0
                and income.get(newer, 0) * eps[newer] > 0):
            income_ratio = income[newer] / income[older]
            implied = income_ratio / eps_ratio
            event.update(incomeRatio=income_ratio, impliedShareRatio=implied)
            if inclusive_range(implied, .8, 1.25):
                event["reason"] = "eps_step_explained_by_income"
        events.append(event)
    return events


def estimate_fields(payload: dict, display_years: set[int], *,
                    diagnostics: list[dict] | None = None) -> dict:
    """display_yearsは来歴検査済みの公式配当年。暦年・予想は渡さない。

    完成したannual/epsで計算し、netIncomeを段差の反証に使う。
    株数の換算や年度の移動はここではしない。
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
    issue = boundary_issue(estimates, total)
    if issue:
        if diagnostics is not None:
            diagnostics.append(issue)
        return {}
    events = eps_step_events(payload, min(estimated), max(estimated))
    cutoffs = [e["years"][1] for e in events if e["reason"] == "eps_integer_step"]
    if cutoffs:
        # 複数の段差がある場合は最も新しい段差の新しい側だけを残す。
        cutoff = max(cutoffs)
        removed = sorted(y for y in estimated if y < cutoff)
        estimated = {y: v for y, v in estimated.items() if y >= cutoff}
        for event in events:
            if event["reason"] == "eps_integer_step":
                event["removedYears"] = [y for y in removed if y < event["years"][1]]
    if diagnostics is not None:
        diagnostics.extend(events)
    display = dict(estimated)
    display.update({y: v for y, v in total.items() if 0 <= v <= 1000})
    return {"payoutRatioEstimated": estimated, "payoutRatioDisplay": display,
            "payoutRatioSource": {y: "total_cash_paid" if y in total else "dps_eps_estimate"
                                  for y in display}}
