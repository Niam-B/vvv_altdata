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
    city_page_store_links, is_city_page, norm_store_url, parse_take5_store, sitemap_locs, state_abbr,
    store_key, take5_has_data,
)

FX = ROOT / "tests" / "fixtures"
URL = "https://www.take5.com/locations/alabama/mobile/30030/"


class TestTake5Parser(unittest.TestCase):
    def setUp(self):
        self.html = (FX / "take5_store.html").read_text()

    def test_store_page(self):
        r = parse_take5_store(self.html, URL)
        self.assertEqual(r["store_url"], URL)
        self.assertEqual((r["store_number"], r["state"], r["state_abbr"], r["city"]),
                         ("30030", "alabama", "AL", "mobile"))
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
        r = parse_take5_store(html, "https://www.take5.com/locations/new-york/albany/30123/")
        self.assertEqual((r["state"], r["state_abbr"], r["store_number"]), ("new-york", "NY", "30123"))
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
        self.assertEqual(stores, ["https://www.take5.com/locations/alabama/mobile/30030/",
                                  "https://www.take5.com/locations/new-york/albany/30123/"])
        cities = sorted(norm_store_url(u) for u in locs if not store_key(u) and is_city_page(u))
        self.assertEqual(cities, ["https://www.take5.com/locations/alabama/mobile/",
                                  "https://www.take5.com/locations/north-carolina/charlotte/"])

    def test_store_key_slugs(self):
        self.assertEqual(store_key("https://www.take5.com/locations/north-carolina/charlotte/31001/"),
                         ("north-carolina", "charlotte", "31001"))
        self.assertEqual(store_key("https://www.take5.com/locations/Alabama/Mobile/30030"),
                         ("alabama", "Mobile", "30030"))
        self.assertIsNone(store_key("https://www.take5.com/locations/alabama/mobile/"))
        self.assertIsNone(store_key("https://www.take5.com/locations/north-carolina/charlotte/coupons/"))

    def test_state_abbr(self):
        self.assertEqual(state_abbr("alabama"), "AL")
        self.assertEqual(state_abbr("new-york"), "NY")
        self.assertEqual(state_abbr("north-carolina"), "NC")
        self.assertEqual(state_abbr("district-of-columbia"), "DC")
        self.assertEqual(state_abbr("tx"), "TX")
        self.assertEqual(state_abbr("narnia"), "")
        from collectors.parse_take5 import STATE_ABBR
        self.assertEqual(len(set(STATE_ABBR.values())), 51)  # 50 states + DC

    def test_city_page_fallback_links(self):
        base = "https://www.take5.com/locations/north-carolina/charlotte/"
        links = city_page_store_links((FX / "take5_city_page.html").read_text(), base)
        self.assertEqual(links, [base + "31001/", base + "31002/", base + "31003/"])


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


class TestTake5Discovery(unittest.TestCase):
    """discover(): sitemap < 1,000 store URLs -> city pages are crawled; results de-duplicated."""

    def test_city_page_fallback(self):
        import collectors.take5_master as tm

        sitemap = (FX / "take5_sitemap_locations.xml").read_bytes()
        city_html = (FX / "take5_city_page.html").read_text()
        # the Mobile page re-lists the sitemap's store (dup) plus one new store
        mobile_html = '<a href="/locations/alabama/mobile/30030/">dup</a><a href="30031/">new</a>'
        fetched = []

        class FakeFetcher:
            def get_bytes(self, url):
                return sitemap if url == tm.SITEMAP else None

            def get(self, url, has_data=None):
                fetched.append(url)
                return {"https://www.take5.com/locations/north-carolina/charlotte/": city_html,
                        "https://www.take5.com/locations/alabama/mobile/": mobile_html}.get(url)

        urls = tm.discover(FakeFetcher())
        self.assertEqual(sorted(fetched), ["https://www.take5.com/locations/alabama/mobile/",
                                           "https://www.take5.com/locations/north-carolina/charlotte/"])
        self.assertEqual(urls, sorted(set(urls)))
        self.assertEqual(urls, [
            "https://www.take5.com/locations/alabama/mobile/30030/",
            "https://www.take5.com/locations/alabama/mobile/30031/",
            "https://www.take5.com/locations/new-york/albany/30123/",
            "https://www.take5.com/locations/north-carolina/charlotte/31001/",
            "https://www.take5.com/locations/north-carolina/charlotte/31002/",
            "https://www.take5.com/locations/north-carolina/charlotte/31003/",
        ])

    def test_no_fallback_when_sitemap_is_large(self):
        import collectors.take5_master as tm

        locs = "".join(f"<url><loc>https://www.take5.com/locations/texas/austin/{i}/</loc></url>"
                       f"<url><loc>https://www.take5.com/locations/texas/c{i}/</loc></url>"
                       for i in range(1000))
        fetched = []

        class FakeFetcher:
            def get_bytes(self, url):
                return f"<urlset>{locs}</urlset>".encode()

            def get(self, url, has_data=None):
                fetched.append(url)

        self.assertEqual(len(tm.discover(FakeFetcher())), 1000)
        self.assertEqual(fetched, [])
