"""Temporary yield holds and monitoring; no dividend conversion."""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from fractions import Fraction
from pathlib import Path
from zoneinfo import ZoneInfo


class PublicEventsError(ValueError):
    pass


def summary(message, warning=False):
    print(("::warning::" if warning else "") + message)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as out:
            out.write(message + "\n\n")


def iso_day(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("invalid date")
    return date.fromisoformat(value)


def positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def load_public_events(path, today):
    """Presence of the receipt permanently ends migration, even if it is corrupt."""
    received = path.with_name("public_stock_events.received.json").exists()
    reason = "public_stock_events_invalid"
    try:
        raw = path.read_text(encoding="utf-8")
        doc = json.loads(raw)
        if (not isinstance(doc, dict)
                or set(doc) != {"schemaVersion", "asOfDate", "sourceSha256", "events"}
                or type(doc["schemaVersion"]) is not int or doc["schemaVersion"] != 1
                or not isinstance(doc["sourceSha256"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", doc["sourceSha256"])
                or not isinstance(doc["events"], list)):
            raise ValueError("invalid schema")
        as_of = iso_day(doc["asOfDate"])
        if as_of > today:
            raise ValueError("future asOfDate")
        result = {}
        for row in doc["events"]:
            if (not isinstance(row, dict)
                    or set(row) != {"code", "effectiveDate", "oldShares", "newShares",
                                    "kind", "recordDate", "sourceDocId"}
                    or not isinstance(row["code"], str)
                    or not re.fullmatch(r"[0-9A-Z]{4}", row["code"])
                    or not positive(row["oldShares"]) or not positive(row["newShares"])
                    or row["oldShares"] == row["newShares"]
                    or row["kind"] != ("split" if row["newShares"] > row["oldShares"] else "consolidation")
                    or row["recordDate"] is not None
                    or (row["sourceDocId"] is not None and
                        (not isinstance(row["sourceDocId"], str) or
                         not re.fullmatch(r"S100[A-Z0-9]{4}", row["sourceDocId"])))):
                raise ValueError("invalid event")
            iso_day(row["effectiveDate"])
            result.setdefault(row["code"], []).append(row)
        if (today - as_of).days >= 3:
            reason = "public_stock_events_stale"
            raise ValueError("stale events")
        return result
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
        if isinstance(exc, FileNotFoundError):
            reason = "public_stock_events_missing"
        if received:
            summary("公開用株式イベントの異常により公開を停止: " + reason, warning=True)
            raise PublicEventsError(reason) from None
        summary("公開用株式イベントは初回配送前のためガード未適用: " + reason, warning=True)
        return {}


def basis_date(record):
    record = record if isinstance(record, dict) else {}
    source = record.get("forecastSource")
    values = [source.get("disclosureDate") if isinstance(source, dict) else None,
              record.get("lastFetchedAt")]
    for value in values:
        if not isinstance(value, str) or not value.strip():
            continue
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(value)
            except (ValueError, TypeError, OverflowError):
                continue
        try:
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(ZoneInfo("Asia/Tokyo"))
            return parsed.date()
        except (ValueError, OverflowError):
            continue
    return None


def price_as_of_day(value):
    try:
        return iso_day(value)
    except ValueError:
        return None


def forecast_hold_reason(record, events, today, *, forecast_basis=None, price_as_of=None):
    basis = basis_date(record)
    upper = today + timedelta(days=14)
    lower = basis if basis is not None else today - timedelta(days=400)
    in_window = [
        iso_day(event["effectiveDate"]) for event in events
        if iso_day(event["effectiveDate"]) <= upper
        and (lower < iso_day(event["effectiveDate"]) if basis is not None
             else lower <= iso_day(event["effectiveDate"]))]
    # A forecast already converted to the post-event basis is safe once the
    # price is dated on/after the effective date (the price is then surely on
    # the new basis). Before that, keep holding: the price may still be old.
    # With two or more events in the window we cannot tell which one the
    # conversion covered, so the exemption is not applied.
    exempt = (forecast_basis in {
                  "post_split_adjusted", "post_split_composed",
                  "post_split_adjusted_ledger", "post_split_composed_ledger",
                  "post_split_single_basis_ledger",
              } and price_as_of is not None
              and len(in_window) == 1)
    for effective in in_window:
        if exempt and effective <= price_as_of:
            continue
        return "forecast_basis_unknown" if basis is None else "stock_event_after_forecast"
    return None


def guard_events(existing, public):
    """Guard-only union. Never feed these records to split_adjustment()."""
    def key(event):
        return (event["securityCode"], event["effectiveDate"],
                Fraction(str(event["newShares"])) / Fraction(str(event["oldShares"])))
    merged, seen = [], set()
    converted = [
        # Existing guard calls both splits and consolidations 'split'.
        dict(securityCode=row["code"], effectiveDate=row["effectiveDate"],
             oldShares=row["oldShares"], newShares=row["newShares"], action="split")
        for row in public]
    for event in [*existing, *converted]:
        identity = key(event)
        if identity not in seen:
            merged.append(event)
            seen.add(identity)
    return merged


def hold_actual(reason):
    return reason == "kouhaitou_split_unreflected"


def previous_payloads(path):
    if not path.exists():
        summary("急低下監視: 前回公開ストアなし（初回は比較なし）", warning=True)
        return {}
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
            return {code: json.loads(raw) for code, raw in conn.execute("SELECT code,payload FROM stocks")}
    except (sqlite3.Error, ValueError):
        summary("急低下監視: 前回公開ストアを読めないため比較なし", warning=True)
        return {}


def price_time(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y/%m/%d %H:%M:%S")
    except ValueError:
        return None


def sudden_drop(previous, basis, current, updated):
    """Compare actual yields, same source/year, from strictly newer price data."""
    if not isinstance(previous, dict) or not positive(previous.get("dividendYield")):
        return None
    old_basis = previous.get("dividendYieldBasis") or {}
    if (not isinstance(old_basis, dict) or basis.get("source") != "fiscal_series"
            or old_basis.get("source") != "fiscal_series"
            or basis.get("fiscalYear") != old_basis.get("fiscalYear")):
        return None
    before_time, after_time = price_time(previous.get("dailyPricesUpdated")), price_time(updated)
    if before_time is None or after_time is None or after_time <= before_time:
        return None
    dividend, price = old_basis.get("annualDividend"), previous.get("price")
    if not positive(dividend) or not positive(price) or not positive(current):
        return None
    before = dividend / price * 100
    if not positive(before):
        return None
    ratio = current / before
    if 0 < ratio <= 0.5:
        return dict(previousYield=before, currentYield=current, ratio=ratio)
    return None
