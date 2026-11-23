"""Store de-duplication: same store under two URLs (trailing/inner periods in the slug)."""
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from collectors.common import dedupe_stores, dedupe_urls, store_url_key  # noqa: E402

B = "https://store.vioc.com"
ROWS = [
    # trailing period, both parsed (same place_id) -> keep URL without the period
    {"store_url": f"{B}/ca/bakersfield/10115-rosedale-hwy.", "place_id": "P1", "lat": "35.38324",
     "lon": "-119.1", "rating": "4.5", "review_count": "900"},
    {"store_url": f"{B}/ca/bakersfield/10115-rosedale-hwy", "place_id": "P1", "lat": "35.38324",
     "lon": "-119.1", "rating": "4.5", "review_count": "900"},
    # periods inside the slug -> keep the one without periods
    {"store_url": f"{B}/or/lincoln-city/1205-s.-w.-highway-101", "place_id": "P2", "lat": "44.95865",
     "lon": "-124.01413", "rating": "4.7", "review_count": "674"},
    {"store_url": f"{B}/or/lincoln-city/1205-s--w--highway-101", "place_id": "P2", "lat": "44.95865",
     "lon": "-124.01413", "rating": "4.7", "review_count": "674"},
    # no place_id on either: lat/lon fallback; the kept (period-free) row is missing rating -> filled
    {"store_url": f"{B}/id/boise/7585-w-fairview-ave", "place_id": "", "lat": "43.61895",
     "lon": "-116.27715", "rating": "", "review_count": ""},
    {"store_url": f"{B}/id/boise/7585-w.-fairview-ave.", "place_id": "", "lat": "43.618951",
     "lon": "-116.277149", "rating": "4.2", "review_count": "300"},
    # dotted page failed to parse (no place_id, no lat/lon): matched by period-free slug
    {"store_url": f"{B}/fl/jacksonville/7780-normandy-blvd.", "place_id": "", "lat": "", "lon": "",
     "rating": "", "review_count": ""},
    {"store_url": f"{B}/fl/jacksonville/7780-normandy-blvd", "place_id": "P4", "lat": "30.294794",
     "lon": "-81.776602", "rating": "4.4", "review_count": "500"},
    # distinct stores: same city, ~0.5-1.2 mi from Rosedale, different place_id -> both kept
    {"store_url": f"{B}/ca/bakersfield/1407-columbus-st", "place_id": "P5", "lat": "35.39",
     "lon": "-119.1", "rating": "4.1", "review_count": "100"},
    {"store_url": f"{B}/ca/bakersfield/2000-oak-st", "place_id": "P6", "lat": "35.40", "lon": "-119.1",
     "rating": "4.0", "review_count": "50"},
]


class TestDedupe(unittest.TestCase):
    def test_dedupe_stores(self):
        kept, alias = dedupe_stores(ROWS)
        urls = [r["store_url"] for r in kept]
        self.assertEqual(urls, [
            f"{B}/ca/bakersfield/10115-rosedale-hwy",
            f"{B}/or/lincoln-city/1205-s--w--highway-101",
            f"{B}/id/boise/7585-w-fairview-ave",
            f"{B}/fl/jacksonville/7780-normandy-blvd",
            f"{B}/ca/bakersfield/1407-columbus-st",
            f"{B}/ca/bakersfield/2000-oak-st",
        ])
        self.assertTrue(all(not u.endswith(".") for u in urls))
        self.assertEqual(alias[f"{B}/ca/bakersfield/10115-rosedale-hwy."], f"{B}/ca/bakersfield/10115-rosedale-hwy")
        self.assertEqual(alias[f"{B}/fl/jacksonville/7780-normandy-blvd."], f"{B}/fl/jacksonville/7780-normandy-blvd")
        self.assertEqual(len(alias), len(ROWS))  # every input URL is mapped
        boise = kept[2]
        self.assertEqual((boise["rating"], boise["review_count"]), ("4.2", "300"))  # filled from duplicate
        self.assertEqual(dedupe_stores(kept)[0], kept)  # idempotent

    def test_dedupe_urls_sample(self):
        _, alias = dedupe_stores(ROWS)
        sample = [f"{B}/ca/bakersfield/10115-rosedale-hwy.", f"{B}/ca/bakersfield/10115-rosedale-hwy",
                  f"{B}/ca/bakersfield/2000-oak-st", f"{B}/tx/new/1-main-st.", f"{B}/tx/new/1-main-st"]
        self.assertEqual(dedupe_urls(sample, alias), [
            f"{B}/ca/bakersfield/10115-rosedale-hwy", f"{B}/ca/bakersfield/2000-oak-st",
            f"{B}/tx/new/1-main-st."])  # unknown pair still collapsed by URL key
        self.assertEqual(store_url_key(f"{B}/a/b/1-s.-w.-x"), store_url_key(f"{B}/a/b/1-s--w--x."))

    def test_wait_times_city_rows(self):
        from collectors.wait_times import city_rows
        _, alias = dedupe_stores(ROWS)
        city = f"{B}/ca/bakersfield/"
        parsed = [{"store_url": city + "10115-rosedale-hwy", "wait_bucket": "0-10", "hours_text": ""},
                  {"store_url": city + "10115-rosedale-hwy.", "wait_bucket": "0-10", "hours_text": ""},
                  {"store_url": city + "1407-columbus-st", "wait_bucket": "", "hours_text": "Closed"}]
        known = {city + "10115-rosedale-hwy", city + "1407-columbus-st", city + "2000-oak-st"}
        rows = city_rows(parsed, known, alias, "T")
        self.assertEqual([(r["store_url"].rsplit("/", 1)[1], r["status"]) for r in rows],
                         [("10115-rosedale-hwy", "ok"), ("1407-columbus-st", "closed"), ("2000-oak-st", "missing")])

    def test_competition_counts_each_store_once(self):
        import analysis.competition as comp
        tmp = Path(tempfile.mkdtemp())
        (tmp / "ref").mkdir()
        pd.DataFrame([{**r, "name": "VIOC", "address": "", "state": "CA", "city": "x"} for r in ROWS]) \
            .to_csv(tmp / "ref" / "stores.csv", index=False)
        pd.DataFrame([{"store_url": "t1", "lat": 35.38324, "lon": -119.1, "google_rating": 4.6,
                       "review_count": 10, "coupon_text": "$15 Off"}]).to_csv(tmp / "ref" / "take5_stores.csv",
                                                                             index=False)
        old = comp.REF, comp.DERIVED, comp.ROOT
        comp.REF, comp.DERIVED, comp.ROOT = tmp / "ref", tmp / "derived", tmp
        try:
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(comp.main(), 0)
        finally:
            comp.REF, comp.DERIVED, comp.ROOT = old
        out = pd.read_csv(tmp / "derived" / "vvv_competition.csv").set_index("store_url")
        self.assertEqual(len(out), 6)
        self.assertFalse(any(u.endswith(".") for u in out.index))
        # Rosedale's neighbours within 3 mi: the two other distinct Bakersfield stores, not its own
        # duplicate URL (which would make it 3)
        self.assertEqual(out.loc[f"{B}/ca/bakersfield/10115-rosedale-hwy", "vvv_within_3mi"], 2)


class TestCommittedStores(unittest.TestCase):
    """Regression guard on the committed reference file."""

    def test_stores_csv_has_no_duplicates(self):
        p = ROOT / "data" / "ref" / "stores.csv"
        if not p.exists():
            self.skipTest("no stores.csv")
        s = pd.read_csv(p, dtype=str, keep_default_na=False)
        self.assertEqual(s["store_url"].map(store_url_key).duplicated().sum(), 0)
        pid = s.loc[s["place_id"] != "", "place_id"]
        self.assertEqual(pid.duplicated().sum(), 0)
        self.assertFalse(s["store_url"].str.endswith(".").any())


if __name__ == "__main__":
    unittest.main()
