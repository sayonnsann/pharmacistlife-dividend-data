"""Validate reviewed financial overlays without touching dividend publication rules."""
from __future__ import annotations
import math

FIELDS = frozenset({
    "revenue", "netIncome", "totalAssets", "roe", "roa", "equityRatio", "deRatio",
    "eps", "bps", "per", "operatingCashFlow", "investingCashFlow", "financingCashFlow",
    "cashAndEquivalents", "interestBearingDebt", "numberOfEmployees", "doe", "payoutRatioTotalBased",
})


def prepare_financial_display(financial: dict) -> dict:
    result = dict(financial)
    marks = financial.get("financialBasisByYear") or {}
    overlay = financial.get("basisSwitchMetrics") or {}
    if not isinstance(marks, dict) or not isinstance(overlay, dict):
        raise ValueError("財務の基準情報がobjectではありません")
    # A reviewed period can only enter through the marked overlay, even if a
    # stale producer also included it in the main/legacy transition series.
    for section in (None, "basisTransitionMetrics"):
        values = result if section is None else dict(result.get(section) or {})
        for field in FIELDS:
            if isinstance(values.get(field), dict):
                values[field] = {y: v for y, v in values[field].items() if y not in marks}
        if section and section in result:
            result[section] = values
    safe = {}
    for field, series in overlay.items():
        if field not in FIELDS or not isinstance(series, dict):
            raise ValueError("財務の切替系列に許可していない項目があります")
        safe[field] = {}
        for year, value in series.items():
            mark = marks.get(year) or {}
            if mark.get("decision") not in {"connect", "separate"}:
                continue
            if (mark.get("basis") not in {"consolidated", "nonConsolidated"}
                    or not isinstance(mark.get("note"), str) or not mark["note"].strip()
                    or field not in (mark.get("fields") or [])):
                raise ValueError("財務の切替値に基準・注記がありません")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError("財務の切替値が有限の数値ではありません")
            safe[field][year] = value
    if "basisSwitchMetrics" in result:
        result["basisSwitchMetrics"] = safe
    return result
