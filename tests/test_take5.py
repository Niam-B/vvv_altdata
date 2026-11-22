"""Take 5 parser + competition tests (fixtures are synthetic; see tests/fixtures/take5_store.html)."""
import gzip
import sys
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from analysis.competition import compute, haversine_matrix, offer_amounts  # noqa: E402
from collectors.parse_take5 import (  # noqa: E402
    norm_store_url, parse_take5_store, sitemap_locs, store_key, take5_has_data,
)

FX = ROOT / "tests" / "fixtures"
URL = "https://www.take5.com/locations/tx/austin/1042/"


class TestTake5Parser(unittest.TestCase):
    def setUp(self):
        self.html = (FX / "take5_store.html").read_text()

    def test_store_page(self):
        r = parse_take5_store(self.html, URL)
        self.assertEqual(r["store_url"], URL)
        self.assertEqual((r["store_number"], r["state"], r["city"]), ("1042", "TX", "austin"))
        self.assertEqual(r["street"], "1234 S Lamar Blvd")
        self.assertEqual(r["zip"], "78704")
        self.assertEqual(r["phone"], "(512) 555-0123")
        self.assertEqual((r["lat"], r["lon"]), (30.2501, -97.7653))
        self.assertEqual((r["google_rating"], r["review_count"]), (4.6, 1575))
        self.assertIn("Monday 7:30 AM - 7:00 PM", r["hours_text"])
        self.assertIn("$15 Off", r["coupon_text"])
        self.assertIn("Core, Complete or Ultimate", r["coupon_text"])
        self.assertIn("$10 Off", r["coupon_text"])
        self.assertTrue(take5_has_data(self.html))

    def test_static_map_fallback_and_page_text_offers(self):
        html = ('<img src="https://maps.googleapis.com/maps/api/staticmap?center=41.1%2C-81.5">'
                '<p>4.8 based on 212 Reviews</p><p>Save $20 Off a full service oil change today.</p>')
        r = parse_take5_store(html, "https://www.take5.com/locations/oh/akron/5/")
        self.assertEqual((r["lat"], r["lon"]), (41.1, -81.5))
        self.assertEqual((r["google_rating"], r["review_count"]), (4.8, 212))
        self.assertIn("$20 Off", r["coupon_text"])

    def test_sitemap(self):
        locs, is_index = sitemap_locs((FX / "take5_sitemap_index.xml").read_bytes())
        self.assertTrue(is_index)
        self.assertEqual(locs, ["https://www.take5.com/sitemap-locations.xml"])
        body = gzip.compress((FX / "take5_sitemap_locations.xml").read_bytes())
        locs, is_index = sitemap_locs(body)
        self.assertFalse(is_index)
        stores = sorted(norm_store_url(u) for u in locs if store_key(u))
        self.assertEqual(stores, ["https://www.take5.com/locations/tx/austin/1042/",
                                  "https://www.take5.com/locations/tx/san-antonio/877/"])


class TestCompetition(unittest.TestCase):
    def test_haversine(self):
        # 1 degree of latitude ~ 69.09 mi
        self.assertAlmostEqual(haversine_matrix([30.0], [-97.0], [31.0], [-97.0])[0, 0], 69.09, delta=0.1)

    def test_compute(self):
        vvv = pd.DataFrame({"store_url": ["a", "b", "c", "d"], "lat": [30.0, 30.01, 30.5, None],
                            "lon": [-97.0, -97.0, -97.0, None]})
        t5 = pd.DataFrame({"store_url": ["t1", "t2"], "lat": [30.005, 30.04], "lon": [-97.0, -97.0]})
        out = compute(vvv, t5).set_index("store_url")
        # a: t1 0.35 mi, t2 2.76 mi
        self.assertEqual(out.loc["a", "t5_within_1mi"], 1)
        self.assertEqual(out.loc["a", "t5_within_3mi"], 2)
        self.assertAlmostEqual(out.loc["a", "nearest_t5_miles"], 0.345, delta=0.01)
        self.assertEqual(out.loc["a", "nearest_t5_url"], "t1")
        self.assertEqual(out.loc["a", "vvv_within_3mi"], 1)  # b, not itself
        self.assertEqual(out.loc["c", "vvv_within_3mi"], 0)
        self.assertEqual(out.loc["c", "t5_within_5mi"], 0)
        self.assertTrue(pd.isna(out.loc["d", "t5_within_3mi"]))

    def test_offer_amounts(self):
        s = pd.Series(["$15 Off Core | $10 Off conventional", "$15 off any", None, "20% off"])
        t = offer_amounts(s)
        self.assertEqual(t["$15 off"], 2)
        self.assertEqual(t["$10 off"], 1)
        self.assertEqual(t["20% off"], 1)


if __name__ == "__main__":
    unittest.main()
