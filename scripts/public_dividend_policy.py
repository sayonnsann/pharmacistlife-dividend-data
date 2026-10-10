"""Public dividend projections must never contain calculation-only yearly values."""
from __future__ import annotations

POLICY_ID = "issuer-material-display-exclude-haitoukin-2026-10-06"
DISPLAY_CATEGORIES = frozenset({"a", "b", "c", "d", "f"})
PRIVATE_KEYS = frozenset({"series", "streakSeries", "displaySeries", "yearProvenance",
                          "externalRawValue", "researchAnnual", "seriesValue",
                          "manualDividendAdjustment", "ratioDetail", "displayProvenance",
                          "displayAdoptions", "yuhoDisplayYears"})


def valid_yuho_adoption(value, reference, evidence, adoption):
    """Narrow exception for independent, converted owner-saved annual reports."""
    import math
    from datetime import date
    if not isinstance(adoption, dict) or reference is None:
        return False
    if (adoption.get("status") != "adopt" or adoption.get("converted") != value
            or evidence.get("sourceCategory") != "b"
            or evidence.get("documentType") != "annualSecuritiesReport"
            or evidence.get("acquisitionRoute") != "ownerSavedPdf"
            or not adoption.get("sourceUrl")
            or "有価証券報告書" not in adoption.get("docTitle", "")
            or evidence.get("sourceUrl") != adoption["sourceUrl"]
            or evidence.get("docTitle") != adoption["docTitle"]
            or adoption.get("basis") not in {"実額", "調整済み", "不明"}):
        return False
    raw = adoption.get("raw")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw) or raw < 0:
        return False
    try:
        date.fromisoformat(adoption["asOf"])
    except (KeyError, TypeError, ValueError):
        return False
    return value == 0 if reference == 0 else abs(value / reference - 1) <= .03 + 1e-12


def issuer_eligible(evidence: dict) -> bool:
    return (evidence.get("sourceCategory") in DISPLAY_CATEGORIES
            and all(issuer_eligible(p) for p in evidence.get("inputs", [])))


def financial_dividend_projection(financial: dict) -> dict:
    """Preserve independently sourced financial DPS, excluding explicit e inputs.

    The producer's screen JSON contains official financial observations, not the
    private calculation series. A hidden fiscal dividend year does not make a
    separately sourced official financial DPS observation external.
    """
    result = dict(financial)
    evidence = financial.get("displayProvenance") or {}
    for section in (None, "basisTransitionMetrics"):
        container = financial if section is None else financial.get(section) or {}
        target = result if section is None else dict(container)
        classes = evidence if section is None else evidence.get(section) or {}
        series = container.get("dividendPerShare")
        if isinstance(series, dict):
            years = classes.get("dividendPerShare") or {}
            target["dividendPerShare"] = {y: v for y, v in series.items()
                                         if str(y) not in years or issuer_eligible(years[str(y)])}
        if section is not None and section in result:
            result[section] = target
    result.pop("displayProvenance", None)
    return result


def financial_dividend_years(financial: dict) -> dict[str, set[int]]:
    return {section: {int(y) for y in (container.get("dividendPerShare") or {})}
            for section, container in (("main", financial),
                ("basisTransitionMetrics", financial.get("basisTransitionMetrics") or {}))}


def validate_public_payload(payload: dict, display_years: set[int], reported_years: set[int],
                            *, financial_years: dict[str, set[int]] | None = None) -> None:
    def walk(value):
        if isinstance(value, dict):
            if PRIVATE_KEYS & value.keys():
                raise ValueError("公開payloadに計算用・非公開配当データがあります")
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(payload)
    for key in ("annual", "dividendBreakdown"):
        if any(int(y) not in display_years for y in (payload.get(key) or {})):
            raise ValueError(f"公開{key}に表示対象外の年度があります")
    for section, container in (("main", payload),
            ("basisTransitionMetrics", payload.get("basisTransitionMetrics") or {})):
        allowed = display_years if financial_years is None else financial_years.get(section, set())
        if any(int(y) not in allowed for y in (container.get("dividendPerShare") or {})):
            raise ValueError("公開dividendPerShareに許可していない年度があります")
    for key in ("annualPending", "annualPartial"):
        if any(int(y) in reported_years for y in (payload.get(key) or {})):
            raise ValueError(f"公開{key}から有報済みの年度が復活しています")
    if "confirmedDividend" in payload:
        year = str(payload.get("confirmedFiscalYearEnd") or "")[:4]
        if (payload.get("annualPending", {}).get(year) or {}).get("kind") != "confirmed":
            raise ValueError("公開confirmedDividendから未採用の年度が復活しています")


def period_metadata(display: dict, calculation: dict) -> dict:
    displayed, calculated = set(display), set(calculation)
    hidden = calculated - displayed
    prefix = bool(hidden and displayed and max(hidden) < min(displayed))
    return {"basis": "fiscal", "displayPolicy": POLICY_ID,
            "startYear": min(displayed) if displayed else None,
            "endYear": max(displayed) if displayed else None,
            "calculationStartYear": min(calculated) if calculated else None,
            "calculationEndYear": max(calculated) if calculated else None,
            "usesHiddenYears": bool(hidden), "hiddenYearsArePrefix": prefix}
