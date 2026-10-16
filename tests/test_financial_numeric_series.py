import json
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from financial_numeric_series import NUMERIC_SERIES, numeric_series_errors, validate_financial_record
from public_dividend_policy import validate_public_payload
import build_store


@pytest.mark.parametrize("value", ["見出し", "－", "5.6", True, False,
    float("nan"), float("inf"), -float("inf"), {}, []])
@pytest.mark.parametrize("section", [None, "basisTransitionMetrics", "basisSwitchMetrics"])
def test_public_series_rejects_invalid_cells(value, section):
    metrics = {"roe": {"1999": value}}
    record = {"code": "4547", **(metrics if section is None else {section: metrics})}
    with pytest.raises(ValueError, match="code=4547 year=1999 field=.*roe"):
        validate_financial_record(record)
    with pytest.raises(ValueError, match="finite number or null"):
        validate_public_payload(record, set(), set())


def test_all_current_metrics_accept_finite_numbers_and_null():
    record = {"code": "4547", **{f: {"1999": 0, "2000": -1, "2001": 5.6, "2002": None}
                               for f in NUMERIC_SERIES}}
    validate_financial_record(record)
    validate_public_payload(record, set(), set(), financial_years={"main": {1999, 2000, 2001, 2002}})


def test_new_year_map_is_checked_even_if_all_cells_are_strings():
    with pytest.raises(ValueError, match="newMetric"):
        validate_financial_record({"code": "4547", "newMetric": {"1999": "label"}})


def test_period_evidence_is_not_a_numeric_series():
    record = {"code": "4547", "basisTransition": {"from": "jp", "years": [1999]},
              "financialPeriods": {"1999": {"label": "1999/3"}},
              "financialBasisByYear": {"1999": {"decision": "hide"}},
              "accountingStandards": {"1999": "jp"},
              "displayProvenance": {"roe": {"1999": {"sourceCategory": "b"}}}}
    assert numeric_series_errors(record) == []


def test_store_stops_before_network_calculation_or_database_creation(tmp_path, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("bad financial inputs reached network/calculation")
    monkeypatch.setattr(build_store, "load_daily_prices", unexpected)
    output = tmp_path / "stocks.sqlite"
    with pytest.raises(ValueError, match="4547.*1999.*roe"):
        build_store.create_database(output, [{"code": "4547", "roe": {"1999": "見出し"}}],
                                    {}, [], {}, [], "unused")
    assert not output.exists()


def test_cli_checks_repository_shaped_input_and_exits_nonzero(tmp_path):
    path = tmp_path / "all_financials.json"
    path.write_text(json.dumps([{"code": "4547", "equityRatio": {"1999": "見出し"}}]))
    script = Path(__file__).resolve().parents[1] / "scripts/financial_numeric_series.py"
    result = subprocess.run([sys.executable, str(script), str(path)], capture_output=True, text=True)
    assert result.returncode == 1
    assert "code=4547 year=1999 field=equityRatio" in result.stdout
    path.write_text(json.dumps([{"code": "4547", "equityRatio": {"1999": 70.0}}]))
    result = subprocess.run([sys.executable, str(script), str(path)], capture_output=True, text=True)
    assert result.returncode == 0
