import importlib.util
import json
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "build_store", ROOT / "scripts" / "build_store.py"
)
assert SPEC and SPEC.loader
build_store = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build_store)


def event(
    code: str,
    event_id: str,
    old_shares: int,
    new_shares: int,
    *,
    eps_adjusted: bool,
    effective_date: str = "2026-07-01",
) -> dict:
    return {
        "eventId": event_id,
        "securityCode": code,
        "action": "split",
        "oldShares": old_shares,
        "newShares": new_shares,
        "effectiveDate": effective_date,
        "status": "confirmed",
        "epsAdjusted": eps_adjusted,
        "source": {
            "url": f"https://example.com/{event_id}.pdf",
            "type": "issuer_ir",
        },
    }


class SplitAdjustmentTest(unittest.TestCase):
    def build(self, path: Path, actions: dict[str, list[dict]]) -> None:
        financials = [
            {
                "code": "7236",
                "name": "ティラド",
                "eps": {"2026": 1500.0},
                "bps": {"2026": 9000.0},
                "per": {"2026": 5.2},
                "dividendPerShare": {"2026": 560.0},
            },
            {
                "code": "2220",
                "name": "亀田製菓",
                "eps": {"2026": 390.0},
                "bps": {"2026": 1620.0},
                "per": {"2026": 3.7},
                "dividendPerShare": {"2026": 66.0},
            },
            {
                "code": "9999",
                "name": "イベントなし",
                "eps": {"2026": 100.0},
                "bps": {"2026": 500.0},
                "per": {"2026": 10.0},
                "dividendPerShare": {"2026": 20.0},
            },
        ]
        prices = {"7236": 1483.0, "2220": 1272.0, "9999": 1000.0}
        daily_dividends = {
            "7236": 560.0,
            "2220": 66.0,
            "9999": 20.0,
        }
        with mock.patch.object(
            build_store,
            "load_daily_prices",
            return_value=(prices, daily_dividends, "2026-08-03"),
        ):
            build_store.create_database(
                path,
                financials,
                {},
                [],
                {},
                [
                    Path("financials"),
                    Path("sectors"),
                    Path("dividends"),
                    Path("forecasts"),
                ],
                "fixture.csv",
                actions,
                Path("stock_actions.json"),
            )

    @staticmethod
    def rows(path: Path) -> dict[str, tuple]:
        with sqlite3.connect(path) as connection:
            return {
                row[0]: (row[1], row[2], json.loads(row[3]))
                for row in connection.execute(
                    "SELECT code, yield, price, payload FROM stocks ORDER BY code"
                )
            }

    def test_eps_adjusted_branch_and_eventless_stock(self) -> None:
        actions = {
            "7236": [
                event("7236", "ten-for-one", 1, 10, eps_adjusted=False)
            ],
            "2220": [
                event("2220", "three-for-one", 1, 3, eps_adjusted=True)
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            baseline = Path(directory) / "baseline.sqlite"
            adjusted = Path(directory) / "adjusted.sqlite"
            self.build(baseline, {})
            self.build(adjusted, actions)
            before = self.rows(baseline)
            after = self.rows(adjusted)

        tirad = after["7236"][2]
        self.assertEqual(after["7236"][0], 3.78)
        self.assertEqual(tirad["eps"]["2026"], 150.0)
        self.assertEqual(tirad["bps"]["2026"], 900.0)
        self.assertEqual(tirad["dividendPerShare"]["2026"], 56.0)
        self.assertEqual(tirad["per"]["2026"], 5.2)
        self.assertEqual(
            tirad["splitAdjustment"]["events"][0]["sourceUrl"],
            "https://example.com/ten-for-one.pdf",
        )

        kameda = after["2220"][2]
        self.assertEqual(after["2220"][0], 1.73)
        self.assertEqual(kameda["eps"], before["2220"][2]["eps"])
        self.assertEqual(kameda["bps"], before["2220"][2]["bps"])
        self.assertEqual(kameda["per"], before["2220"][2]["per"])
        self.assertEqual(kameda["dividendPerShare"]["2026"], 22.0)

        self.assertEqual(after["9999"], before["9999"])
        self.assertLess(
            tirad["dividendYield"], before["7236"][2]["dividendYield"]
        )
        self.assertLess(
            kameda["dividendYield"], before["2220"][2]["dividendYield"]
        )

    def test_multiple_events_multiply_factors(self) -> None:
        adjustment = build_store.split_adjustment(
            [
                event("1234", "one-to-two", 1, 2, eps_adjusted=False),
                event("1234", "one-to-five", 1, 5, eps_adjusted=True),
            ]
        )
        assert adjustment is not None
        self.assertAlmostEqual(adjustment["dividendFactor"], 0.1)
        self.assertAlmostEqual(adjustment["epsBpsFactor"], 0.5)
        self.assertEqual(len(adjustment["events"]), 2)

    def test_loader_excludes_future_events(self) -> None:
        document = {
            "events": [
                event(
                    "1234",
                    "future",
                    1,
                    2,
                    eps_adjusted=False,
                    effective_date="2026-09-01",
                ),
                event(
                    "1234",
                    "effective",
                    1,
                    2,
                    eps_adjusted=False,
                    effective_date="2026-07-01",
                ),
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "actions.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            loaded = build_store.load_stock_actions(
                path, as_of=date(2026, 8, 3)
            )
        self.assertEqual(
            [item["eventId"] for item in loaded["1234"]], ["effective"]
        )

    def test_manual_actions_have_21_stocks_and_kameda_eps_flag(self) -> None:
        loaded = build_store.load_stock_actions(
            ROOT / "data" / "stock_actions_manual.json",
            as_of=date(2026, 8, 3),
        )
        self.assertEqual(len(loaded), 21)
        self.assertEqual(sum(map(len, loaded.values())), 21)
        self.assertTrue(loaded["2220"][0]["epsAdjusted"])
        self.assertTrue(
            all(
                not item["epsAdjusted"]
                for code, events in loaded.items()
                if code != "2220"
                for item in events
            )
        )


if __name__ == "__main__":
    unittest.main()
