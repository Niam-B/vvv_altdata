"""Parser tests on synthetic HTML (the real site was not reachable when these were written).

Replace/extend the fixtures with real pages saved via SAVE_RAW=1 once available.
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collectors.common import stratified_sample  # noqa: E402
from collectors.parse import (  # noqa: E402
    norm_wait, parse_city, parse_index, parse_jobs, parse_jobs_total, parse_state, parse_store,
)

B = "https://store.vioc.com"

INDEX = '<a href="/ky">Kentucky</a><a href="tx.html">Texas</a><a href="/about">x</a>' \
        '<a href="https://other.com/oh">no</a>'
STATE = '<a href="/ky/louisville">Louisville</a><a href="ky/lexington">Lexington</a>' \
        '<a href="/ky/paducah/123-main-st">Paducah store</a><a href="/tx/austin">no</a>'
CITY = """
<ul>
 <li class="loc">
   <h3><a href="/ky/louisville/4500-dixie-hwy">Valvoline Instant Oil Change</a></h3>
   <address>4500 Dixie Hwy, Louisville, KY 40216</address>
   <div class="wait">Current wait: 10-20 Minutes</div>
   <div class="hours">Open until 7:00 PM</div>
   <ul class="services"><li>Oil Change</li><li>Battery</li></ul>
   <a href="/ky/louisville/4500-dixie-hwy">Store Details</a>
 </li>
 <li class="loc">
   <h3><a href="/ky/louisville/100-bardstown-rd">Valvoline Instant Oil Change</a></h3>
   <address>100 Bardstown Rd, Louisville, KY 40204</address>
   <div class="wait">20+ Minutes</div>
 </li>
 <li class="loc">
   <h3><a href="/ky/louisville/9-closed-st">VIOC</a></h3>
   <div class="hours">Closed today</div>
 </li>
</ul>"""
STORE = """<html><head>
<meta property="place:location:latitude" content="38.2">
<meta property="place:location:longitude" content="-85.8">
<script type="application/ld+json">{"@context":"https://schema.org","@graph":[
 {"@type":"AutoRepair","name":"Valvoline Instant Oil Change","address":{"streetAddress":"4500 Dixie Hwy",
  "addressLocality":"Louisville","addressRegion":"KY","postalCode":"40216"},
  "aggregateRating":{"ratingValue":"4.7","reviewCount":"1,234"}}]}</script>
</head><body>
<h1>Valvoline Instant Oil Change - Dixie Hwy</h1>
<a href="https://search.google.com/local/writereview?placeid=ChIJabcdefghijklmnop1234">Write a review</a>
<div class="coupons">
  <div class="coupon">$10 off any full-service oil change. Expires 12/31/2026</div>
  <div class="coupon">50% off. New customers only. Valid through Jan 15, 2027</div>
</div></body></html>"""
JOBS = """<div>Showing 1-20 of 1,234 jobs</div>
<div class="job"><a href="/job/123/technician">Automotive Technician</a>
 <span class="location">Louisville, KY</span><span class="tag">VIOCF</span></div>
<div class="job"><a href="/job/456/manager">Store Manager</a><span class="location">Austin, TX</span></div>
<a href="/service-center-jobs/page/2">Next</a>"""


class TestParse(unittest.TestCase):
    def test_index(self):
        self.assertEqual(parse_index(INDEX, B + "/"), [B + "/ky", B + "/tx"])

    def test_state(self):
        cities, direct = parse_state(STATE, B + "/ky", "ky")
        self.assertEqual(cities, [B + "/ky/lexington", B + "/ky/louisville"])
        self.assertEqual(direct, [B + "/ky/paducah/123-main-st"])

    def test_city(self):
        rows = {r["store_url"].rsplit("/", 1)[1]: r for r in parse_city(CITY, B + "/ky/louisville")}
        self.assertEqual(len(rows), 3)
        a = rows["4500-dixie-hwy"]
        self.assertEqual(a["wait_bucket"], "10-20")
        self.assertIn("4500 Dixie Hwy", a["address"])
        self.assertEqual(a["services"], "Oil Change|Battery")
        self.assertEqual(rows["100-bardstown-rd"]["wait_bucket"], "20+")
        self.assertEqual(rows["9-closed-st"]["wait_bucket"], "")
        self.assertIn("Closed", rows["9-closed-st"]["hours_text"])

    def test_wait_norm(self):
        self.assertEqual(norm_wait("0-10 Minutes"), "0-10")
        self.assertEqual(norm_wait("20 + Minutes"), "20+")

    def test_store(self):
        r = parse_store(STORE, B + "/ky/louisville/4500-dixie-hwy")
        self.assertEqual((r["lat"], r["lon"]), (38.2, -85.8))
        self.assertEqual(r["place_id"], "ChIJabcdefghijklmnop1234")
        self.assertEqual((r["rating"], r["review_count"]), (4.7, 1234))
        self.assertEqual(len(r["coupons"]), 2)
        c0, c1 = r["coupons"]
        self.assertEqual(c0["amount"], "$10 off")
        self.assertEqual(c0["expiry"], "12/31/2026")
        self.assertFalse(c0["new_customers_only"])
        self.assertTrue(c1["new_customers_only"])
        self.assertEqual(c1["expiry"], "Jan 15, 2027")
        json.dumps(r["coupons"])

    def test_jobs(self):
        self.assertEqual(parse_jobs_total(JOBS), 1234)
        jobs = parse_jobs(JOBS, "https://jobs.vioc.com/service-center-jobs")
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0]["label"], "VIOCF")
        self.assertEqual(jobs[0]["address"], "Louisville, KY")
        self.assertEqual(jobs[1]["label"], "")

    def test_sample_deterministic(self):
        items = [{"store_url": f"u{i}", "state": "S%d" % (i % 7)} for i in range(1000)]
        a, b = stratified_sample(items, "state", 400), stratified_sample(items, "state", 400)
        self.assertEqual(len(a), 400)
        self.assertEqual(a, b)
        self.assertEqual(len({x["state"] for x in a}), 7)


if __name__ == "__main__":
    unittest.main()
