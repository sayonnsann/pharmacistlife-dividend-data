"""Validate reviewed financial overlays without touching dividend publication rules."""
from __future__ import annotations
import math
from datetime import date

FIELDS = frozenset({
    "revenue", "netIncome", "totalAssets", "roe", "roa", "equityRatio", "deRatio",
    "eps", "bps", "per", "operatingCashFlow", "investingCashFlow", "financingCashFlow",
    "cashAndEquivalents", "interestBearingDebt", "numberOfEmployees", "doe", "payoutRatioTotalBased",
})


def _event_id_set(ids):
    if (not isinstance(ids, list)
            or any(not isinstance(eid, str) or not eid.strip() for eid in ids)
            or len(ids) != len(set(ids))):
        raise ValueError("不正なイベントID集合です")
    return set(ids)


def _value_basis_applied(proof, event_ids, mark, basis_date):
    # A present but incomplete proof must never fall back to the filing window.
    if "valueBasis" not in proof:
        return None
    basis = proof["valueBasis"]
    if (not isinstance(basis, dict) or basis.get("status") != "determined"
            or not isinstance(basis.get("observationId"), str) or not basis["observationId"].strip()
            or basis.get("holdReason") or basis.get("reversedEventIds")
            or mark.get("basisDate") != basis_date):
        raise ValueError("値ごとの分割反映基準が未確定です")
    reflected = _event_id_set(basis["reflectedEventIds"])
    target = _event_id_set(basis["targetEventIds"])
    applied = _event_id_set(basis["appliedEventIds"])
    if not reflected <= target or applied != target - reflected or applied != event_ids:
        raise ValueError("値ごとの分割反映基準の集合が一致しません")
    return applied


def per_share_basis_date(financial: dict) -> str | None:
    basis = financial.get("basisSwitchPerShareBasis")
    if basis is None:
        return None
    try:
        filing = date.fromisoformat(basis["filingDate"])
        end = date.fromisoformat(basis["fiscalYearEnd"])
        if filing < end or not basis.get("docID") or not basis.get("source"):
            raise ValueError()
    except (TypeError, KeyError, ValueError):
        raise ValueError("財務EPS/BPSの基準提出日が不正です") from None
    return filing.isoformat()


def prepare_financial_display(financial: dict) -> dict:
    result = dict(financial)
    marks = financial.get("financialBasisByYear") or {}
    overlay = financial.get("basisSwitchMetrics") or {}
    basis_date = per_share_basis_date(financial)
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
            if field in {"eps", "bps"} and (basis_date is not None or "perShare" in mark):
                proof = (mark.get("perShare") or {}).get(field) or {}
                try:
                    if not isinstance(proof, dict):
                        raise ValueError()
                    source_date = date.fromisoformat(proof["sourceFilingDate"]).isoformat()
                    raw, factor = proof["sourceValue"], proof["factor"]
                    numbers = (raw, factor)
                    events = proof["events"]
                    if not isinstance(events, list) or any(not isinstance(e, dict) for e in events):
                        raise ValueError()
                    event_ids = _event_id_set([e["eventId"] for e in events])
                    applied = _value_basis_applied(proof, event_ids, mark, basis_date)
                    event_valid = all(date.fromisoformat(e["effectiveDate"]).isoformat() <= basis_date
                                and (source_date < date.fromisoformat(e["effectiveDate"]).isoformat()
                                     or (applied is not None and e["eventId"] in applied))
                                and isinstance(e["factor"], (int, float)) and not isinstance(e["factor"], bool)
                                and math.isfinite(e["factor"]) and e["factor"] > 0 for e in events)
                    valid = (basis_date is not None and event_valid
                             and ("basisDate" not in mark or mark["basisDate"] == basis_date)
                             and proof.get("status") == "shown" and proof.get("sourceDocID")
                             and proof.get("source") and source_date <= basis_date
                             and proof.get("targetFilingDate") == basis_date
                             and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                                     and math.isfinite(v) for v in numbers)
                             and factor > 0 and math.isclose(math.prod(e["factor"] for e in events), factor, rel_tol=1e-9)
                             and math.isclose(raw * factor, value, rel_tol=1e-9, abs_tol=1e-9))
                except (TypeError, KeyError, ValueError):
                    valid = False
                if not valid:
                    raise ValueError("財務の切替EPS/BPSに有報・株数基準の証跡がありません")
            safe[field][year] = value
    if "basisSwitchMetrics" in result:
        result["basisSwitchMetrics"] = safe
    return result
