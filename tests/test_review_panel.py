"""Synthetic end-to-end test of analysis/review_panel.py."""
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analysis import review_panel as rp  # noqa: E402

B = "https://store.vioc.com/ky/x/"


class TestPanel(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.hist, self.ref, self.live = self.tmp / "hist", self.tmp / "ref", self.tmp / "live"
        for d in (self.hist, self.ref, self.live / "coupons"):
            d.mkdir(parents=True)
        snaps = []
        # 3 stores, 2019Q1..2023Q4 quarterly at mid-quarter + 1 Breeze store.
        # store a: +100/qtr until 2022, then +80/qtr (=> -20% YoY in 2023)
        quarters = pd.period_range("2019Q1", "2023Q4", freq="Q")
        for s, inc0, inc1 in (("a", 100, 80), ("b", 50, 40), ("c", 30, 24), ("breeze-1", 10, 50)):
            cnt = 1000
            for q in quarters:
                cnt += inc0 if q.year < 2023 else inc1
                d = (q.start_time + pd.Timedelta(days=45)).strftime("%Y-%m-%d")
                snaps.append({"store_url": B + s, "quarter": str(q), "wayback_ts": d.replace("-", "") + "000000",
                              "snapshot_date": d, "review_count": cnt, "rating": 4.5, "status": "ok",
                              "fetched_ts": ""})
        snaps.append({"store_url": B + "a", "quarter": "2018Q4", "wayback_ts": "", "snapshot_date": "2018-11-01",
                      "review_count": "", "rating": "", "status": "no_count", "fetched_ts": ""})
        pd.DataFrame(snaps).to_csv(self.hist / "wayback_snapshots.csv", index=False)
        pd.DataFrame([{"store_url": B + s, "first_capture": fc, "cdx_status": "ok"} for s, fc in
                      (("a", "20170101000000"), ("b", "20210601000000"), ("c", "20240101000000"),
                       ("breeze-1", "20151201000000"))]).to_csv(self.hist / "wayback_cdx.csv", index=False)
        pd.DataFrame([
            {"store_url": B + "a", "name": "VIOC", "address": "1 Main St Louisville KY 40201", "state": "KY"},
            {"store_url": B + "b", "name": "VIOC", "address": "2 Main St Louisville KY 40202", "state": "KY"},
            {"store_url": B + "c", "name": "VIOC", "address": "3 Main St Louisville KY 40203", "state": "KY"},
            {"store_url": B + "breeze-1", "name": "Breeze Auto Care", "address": "4 Main St KY 40204", "state": "KY"},
        ]).to_csv(self.ref / "stores.csv", index=False)
        pd.DataFrame({"zcta": ["40201", "40202", "40203"], "median_hh_income": [30000, 60000, 90000],
                      "acs_year": 2023}).to_csv(self.ref / "zcta_income.csv", index=False)
        # live: store a in 2024Q1 (ts in Feb), +80*... continuing at 80/qtr
        pd.DataFrame([{"ts_utc": "2024-02-14T14:15:00Z", "store_url": B + "a", "rating": 4.5,
                       "review_count": 1000 + 16 * 100 + 4 * 80 + 80, "coupons_json": "[]"}]) \
            .to_csv(self.live / "coupons" / "2024-02-14.csv", index=False)

    def test_run(self):
        stats = rp.run(self.hist, self.ref, self.live, quiet=True)
        panel = pd.read_csv(self.hist / "review_panel_quarterly.csv")
        yoy = pd.read_csv(self.hist / "review_yoy_summary.csv")
        a = panel[panel.store_url == B + "a"].set_index("quarter")
        self.assertAlmostEqual(a.loc["2020Q1", "new_reviews_91d"], 100 * 91 / a.loc["2020Q1", "gap_days"], 3)
        self.assertEqual(a.loc["2024Q1", "source"], "live")
        self.assertTrue(bool(panel[panel.store_url == B + "breeze-1"]["breeze"].all()))
        allq = yoy[(yoy.group_type == "all") & (yoy.quarter == "2023Q3")].iloc[0]
        self.assertEqual(allq["n_stores"], 3)  # breeze excluded
        self.assertAlmostEqual(allq["yoy_agg"], -0.20, delta=0.02)
        self.assertEqual(allq["fiscal_quarter"], "FY2023Q4")
        terc = yoy[(yoy.group_type == "income_tercile") & (yoy.quarter == "2023Q3")]
        self.assertEqual(sorted(terc["group"]), ["T1 low", "T2 mid", "T3 high"])
        age = yoy[(yoy.group_type == "age_bucket") & (yoy.quarter == "2023Q3")]
        self.assertIn("2023+ (newer)", set(age["group"]))
        self.assertEqual(stats["stores"], 4)
        self.assertAlmostEqual(stats["pct_ge12_usable_quarters"], 100.0)
        self.assertTrue(85 <= stats["median_gap_days"] <= 95)


if __name__ == "__main__":
    unittest.main()
