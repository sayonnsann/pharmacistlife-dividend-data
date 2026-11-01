from fiscal_fixtures import annual_report_fixture
import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_store as store
import yield_safety as safety

TODAY = date(2026, 11, 1)


def event(code="6574", day="2026-11-01", old=10, new=1):
    return dict(code=code, effectiveDate=day, oldShares=old, newShares=new,
                kind="split" if new > old else "consolidation", recordDate=None, sourceDocId=None)


def document(events=(), day="2026-11-01"):
    return dict(schemaVersion=1, asOfDate=day, sourceSha256="a" * 64, events=list(events))


def write_events(path, events=(), day="2026-11-01"):
    path.write_text(json.dumps(document(events, day)), encoding="utf-8")


@pytest.mark.parametrize("received", [False, True])
@pytest.mark.parametrize("bad", ["missing", "broken", "schema", "shape", "event", "stale"])
def test_lifecycle(tmp_path, received, bad, monkeypatch):
    path = tmp_path / "public_stock_events.json"
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    if received:
        # Even corrupt receipt contents remain evidence of delivery.
        (tmp_path / "public_stock_events.received.json").write_text("{")
    if bad == "broken":
        path.write_text("{")
    elif bad != "missing":
        doc = document()
        if bad == "schema":
            doc["schemaVersion"] = True
        elif bad == "shape":
            doc["events"] = {}
        elif bad == "event":
            doc["events"] = [{**event(), "newShares": 0}]
        elif bad == "stale":
            doc["asOfDate"] = "2026-10-29"
        path.write_text(json.dumps(doc))
    if received:
        with pytest.raises(safety.PublicEventsError):
            safety.load_public_events(path, TODAY)
        assert "公開を停止" in summary.read_text()
    else:
        assert safety.load_public_events(path, TODAY) == {}
        assert "初回配送前" in summary.read_text()


def test_valid_fresh_input_and_age_two_is_allowed(tmp_path):
    path = tmp_path / "public_stock_events.json"
    (tmp_path / "public_stock_events.received.json").write_text("{}")
    write_events(path, [event()], "2026-10-30")
    assert safety.load_public_events(path, TODAY) == {"6574": [event()]}


@pytest.mark.parametrize("basis,day,expected", [
    ("2026-08-24", "2026-11-01", True), ("2026-11-01", "2026-11-01", False),
    ("2026-11-02", "2026-11-01", False), ("2026-08-24", "2026-11-15", True),
    ("2026-08-24", "2026-11-16", False),
    (None, (TODAY - timedelta(days=400)).isoformat(), True),
    (None, (TODAY - timedelta(days=401)).isoformat(), False),
    (None, "2026-11-15", True), (None, "2026-11-16", False)])
def test_forecast_boundaries(basis, day, expected):
    record = {"forecastSource": {"disclosureDate": basis}}
    assert bool(safety.forecast_hold_reason(record, [event(day=day)], TODAY)) == expected
    assert safety.forecast_hold_reason(record, [], TODAY) is None


def test_basis_precedence_fallback_and_timezone():
    record = dict(forecastSource={"disclosureDate": "bad"}, lastFetchedAt="2026-08-24T12:00:00Z")
    assert safety.basis_date(record) == date(2026, 8, 24)
    record["forecastSource"]["disclosureDate"] = "2026-11-01"
    assert safety.basis_date(record) == TODAY
    assert safety.basis_date({"lastFetchedAt": "Sat, 31 Oct 2026 16:00:00 GMT"}) == TODAY
    assert safety.basis_date({"lastFetchedAt": "bad"}) is None


def test_union_is_guard_only_deduplicated_and_keeps_existing_without_public():
    old = dict(securityCode="6787", effectiveDate="2026-11-02", oldShares=1,
               newShares=2, action="split")
    assert safety.guard_events([old], []) == [old]
    assert safety.guard_events([old], [event("6787", "2026-11-02", 1, 2)]) == [old]
    active = [dict(code="6574", execution_date="2026-10-29", ratio=.1, active=True)]
    assert store.yield_split_guard_reason("6574", 1, 1, [], active) == "kouhaitou_split_unreflected"
    assert safety.guard_events([old, old], [event("6787", "2026-11-02", 2, 4)]) == [old]
    merged = safety.guard_events([old], [event()])
    assert store.yield_split_guard_reason("6574", 1, 1, merged, active) is None
    assert len(merged) == 2 and len([old]) == 1
    assert safety.hold_actual("kouhaitou_split_unreflected")
    assert not safety.hold_actual("split_like_ratio")
    assert not safety.hold_actual(None)


def previous():
    return dict(dividendYield=1, price=100,
                dailyPricesUpdated="2026/10/30 15:30:00",
                dividendYieldBasis=dict(source="fiscal_series", fiscalYear=2026, annualDividend=1))


@pytest.mark.parametrize("current,expected", [(.08, True), (.5, True), (.50001, False),
    (2, False), (0, False), (-1, False), (None, False), (float("nan"), False)])
def test_sudden_drop_threshold(current, expected):
    old = previous()
    assert bool(safety.sudden_drop(old, old["dividendYieldBasis"], current,
                                  "2026/11/01 09:05:00")) == expected


def test_incomparable_previous_is_skipped():
    old = previous()
    for value in (None, 0):
        assert safety.sudden_drop({**old, "dividendYield": value},
            old["dividendYieldBasis"], .08, "2026/11/01 09:05:00") is None
    assert safety.sudden_drop(old, {**old["dividendYieldBasis"], "fiscalYear": 2027},
                              .08, "2026/11/01 09:05:00") is None
    for updated in ("bad", "2026/10/30 15:30:00", "2026/10/29 15:30:00"):
        assert safety.sudden_drop(old, old["dividendYieldBasis"], .08, updated) is None


def build(tmp_path, *, events=None, active=(), forecast=None, price=1000, updated="2026/11/01 09:05:00",
          meta=None):
    public = tmp_path / "public_stock_events.json"
    if events is not None:
        write_events(public, events)
    database, report = tmp_path / "stocks.sqlite", tmp_path / "report.json"
    fiscal = annual_report_fixture({"6574": dict(fiscalMonth=3, series={2026: 1},
        appliedActions=[], externalYears=[], externalSource=None,
        connectionStatus="connected", connectionReason="fixture")})
    with (patch.object(store, "load_daily_prices", return_value=({"6574": price}, {"6574": 1}, updated)),
          patch.object(store, "load_yield_split_adjustments", return_value=list(active)),
          patch.object(store, "load_price_session_meta", return_value=meta),
          patch.object(store, "load_yield_source_year", return_value=2026)):
        store.create_database(database, [dict(code="6574", name="fixture")], {}, {},
            {"6574": forecast} if forecast else {}, [Path("fixture")] * 4, "fixture.csv",
            fiscal_by_code=fiscal, today=TODAY, public_events_path=public,
            yield_guard_report_path=report)
    with sqlite3.connect(database) as conn:
        actual, predicted, raw = conn.execute("SELECT yield,forecast_yield,payload FROM stocks").fetchone()
    return actual, predicted, json.loads(raw), json.loads(report.read_text())


def test_forecast_hold_changes_both_columns_and_preserves_graph(tmp_path):
    fc = dict(forecastDividend=1, forecastFiscalYear=2027, forecastPeriod="2027年3月期",
              forecastSource={"disclosureDate": "2026-08-24"}, lastFetchedAt="2026-08-24")
    baseline = build(tmp_path, events=[], forecast=fc)
    held = build(tmp_path, events=[event()], forecast=fc)
    assert baseline[1] == .1 and baseline[2]["forecastYield"] == .1
    assert held[1] is None and held[2]["forecastYield"] is None
    assert held[0] == baseline[0]
    for key in ("annual", "annualPending", "streakIncrease", "streakNonDecrease", "forecastDividend"):
        assert held[2].get(key) == baseline[2].get(key)


def test_future_meiko_and_unrelated_issuer(tmp_path):
    path = tmp_path / "public_stock_events.json"
    write_events(path, [event("6787", "2026-11-02", 1, 2)])
    events = safety.load_public_events(path, TODAY)
    fc = dict(forecastDividend=160, forecastShareBasis="pre_split",
              forecastSplitFactor=2, forecastSplitEffectiveDate="2026-11-02",
              forecastSource={"disclosureDate": "2026-08-24"})
    assert store.forecast_values(fc, 8556, today=TODAY)[-1] == "pre_split_reported"
    assert safety.forecast_hold_reason(fc, events["6787"], TODAY)
    assert safety.forecast_hold_reason(fc, events.get("6574", []), TODAY) is None


ADJUSTED_FC = dict(forecastSource={"disclosureDate": "2026-08-24"}, lastFetchedAt="2026-10-02")


def test_adjusted_forecast_shown_once_price_is_on_or_after_effective_date():
    # 1904-like: converted forecast, split effective 10/01, price dated 10/30.
    assert safety.forecast_hold_reason(ADJUSTED_FC, [event(day="2026-10-01", old=1, new=4)], TODAY,
        forecast_basis="post_split_adjusted", price_as_of=date(2026, 10, 30)) is None
    assert safety.forecast_hold_reason(ADJUSTED_FC, [event(day="2026-10-30", old=1, new=4)], TODAY,
        forecast_basis="post_split_adjusted", price_as_of=date(2026, 10, 30)) is None


def test_adjusted_forecast_held_while_price_predates_effective_date():
    # 6787-like: effective 11/02, price still dated 10/30 (or unknown).
    split = [event(code="6787", day="2026-11-02", old=1, new=2)]
    for price_as_of in (date(2026, 10, 30), None):
        assert safety.forecast_hold_reason(ADJUSTED_FC, split, TODAY,
            forecast_basis="post_split_adjusted", price_as_of=price_as_of) == "stock_event_after_forecast"


def test_raw_forecast_stays_held_after_effective_date():
    assert safety.forecast_hold_reason(ADJUSTED_FC, [event(day="2026-10-01", old=1, new=4)], TODAY,
        forecast_basis="raw", price_as_of=date(2026, 10, 30)) == "stock_event_after_forecast"


def test_build_passes_price_session_date_to_forecast_hold(tmp_path):
    fc = dict(forecastDividend=1, forecastFiscalYear=2027, forecastPeriod="2027年3月期",
              forecastSource={"disclosureDate": "2026-08-24"}, lastFetchedAt="2026-08-24")
    split = [event(day="2026-10-01", old=1, new=4)]
    with patch.object(store, "forecast_values", return_value=(1, .1, "2027年3月期", "2026-08-24",
                                                              "post_split_adjusted")):
        shown = build(tmp_path, events=split, forecast=fc, meta={"as_of_date": "2026-10-30"})
        held = build(tmp_path, events=split, forecast=fc, meta={"as_of_date": "2026-09-30"})
    assert shown[1] == .1 and "forecastYieldHoldReason" not in shown[2]
    assert held[1] is None and held[2]["forecastYieldHoldReason"] == "stock_event_after_forecast"


def test_actual_hold_works_without_public_and_monitor_runs_before_hold(tmp_path):
    # Seed the same previous published SQLite that the workflows download.
    build(tmp_path, price=100, updated="2026/10/30 15:30:00")
    active = [dict(code="6574", execution_date="2026-10-29", ratio=.1, active=True)]
    held = build(tmp_path, price=1120, active=active)
    assert held[0] is None and held[2]["dividendYield"] is None
    assert held[2]["annual"] == {"2026": 1}
    drop = held[3]["yield_drops"][0]
    assert drop["code"] == "6574" and 0 < drop["ratio"] <= .5


def test_received_missing_stops_before_prices_and_keeps_previous_database(tmp_path):
    database = tmp_path / "stocks.sqlite"
    database.write_bytes(b"previous published store")
    (tmp_path / "public_stock_events.received.json").write_text("{}")
    report = tmp_path / "report.json"
    with patch.object(store, "load_daily_prices") as prices:
        with pytest.raises(safety.PublicEventsError):
            store.create_database(database, [], {}, {}, {}, [Path("fixture")] * 4,
                "fixture.csv", public_events_path=tmp_path / "public_stock_events.json",
                yield_guard_report_path=report)
    prices.assert_not_called()
    assert database.read_bytes() == b"previous published store"
    assert json.loads(report.read_text())["public_stock_events_error"] == "public_stock_events_missing"


def test_unknown_basis_hold_is_counted(tmp_path, monkeypatch):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    held = build(tmp_path, events=[event()], forecast=dict(forecastDividend=1,
                 forecastFiscalYear=2027, lastFetchedAt="bad"))
    assert held[1] is None
    assert "基準日不明: 1社" in summary.read_text()


def test_fetch_previous_never_stops_the_build(tmp_path, monkeypatch, capsys):
    import fetch_previous_store as fetcher
    for key, value in dict(FTP_HOST="example.invalid", FTP_USER="test",
                           FTP_PASS="test", FTP_REMOTE_DIR="/private").items():
        monkeypatch.setenv(key, value)
    with patch.object(fetcher, "download", return_value=78):
        fetcher.fetch_previous(tmp_path / "stocks.sqlite")
    assert "初回は急低下比較なし" in capsys.readouterr().out
    with patch.object(fetcher, "download", return_value=0):
        fetcher.fetch_previous(tmp_path / "stocks.sqlite")
    # 取得失敗は警告だけにし、途中まで書かれた古い比較用ファイルを残さない。
    partial = tmp_path / "stocks.sqlite"
    partial.write_bytes(b"partial")
    with patch.object(fetcher, "download", return_value=67):
        fetcher.fetch_previous(partial)
    assert "取得に失敗しました" in capsys.readouterr().out
    assert not partial.exists()
    monkeypatch.setenv("FTP_REMOTE_DIR", "relative")
    with patch.object(fetcher, "download") as download:
        fetcher.fetch_previous(partial)
    download.assert_not_called()
