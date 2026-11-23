"""One-off Take 5 Oil Change store crawl (competitor universe).

1. www.take5.com/sitemap.xml (follows sitemap indexes, .xml.gz ok) -> store URLs matching
   /locations/{state-slug}/{city}/{digits}/ (e.g. /locations/new-york/albany/30123/).
   If that yields < 1,000 stores, every city page /locations/{state}/{city}/ in the sitemap
   is also fetched and its store links collected (de-duplicated).
2. Each store page (~1 req/s + jitter, retries) -> data/ref/take5_stores.csv

Resumable: rows are appended to data/ref/take5_stores.csv as they are fetched; stores
already in the file are skipped on the next run. Failed fetches are not written, so a
re-run retries them. FRESH=1 starts over. Stops cleanly after MAX_MINUTES (default 270).

Exit 1 if the sitemap yields no store URLs or no store page could be parsed.
"""
# --- bootstrap: collectors/http.py would shadow stdlib `http`; import as a package
import sys, pathlib  # noqa: E401
_HERE = pathlib.Path(__file__).resolve().parent
sys.path[:] = [p for p in sys.path if pathlib.Path(p or ".").resolve() != _HERE]
sys.path.insert(0, str(_HERE.parent))
# ---------------------------------------------------------------------------

import logging
import os
import time
from collections import Counter

from collectors.common import REF, append_rows, read_csv, setup_logging, ts_utc, write_csv
from collectors.http import Fetcher
from collectors.parse_take5 import (
    city_page_store_links, is_city_page, norm_store_url, parse_take5_store, sitemap_locs, store_key,
    take5_has_data,
)

log = logging.getLogger("take5")

SITEMAP = "https://www.take5.com/sitemap.xml"
OUT = REF / "take5_stores.csv"
FIELDS = ["store_url", "store_number", "state", "state_abbr", "city", "street", "zip", "phone", "lat", "lon",
          "google_rating", "review_count", "hours_text", "coupon_text", "crawl_ts"]
MAX_SITEMAPS = 200
MIN_SITEMAP_STORES = 1000  # below this, also crawl city pages for store links


def discover(f: Fetcher) -> list[str]:
    stores, cities = discover_sitemap(f)
    if len(stores) < MIN_SITEMAP_STORES and cities:
        log.info("sitemap gave %d store URLs (< %d); crawling %d city pages for store links",
                 len(stores), MIN_SITEMAP_STORES, len(cities))
        before = len(stores)
        stores |= set(discover_city_pages(f, cities))
        log.info("city-page fallback added %d store URLs (total %d)", len(stores) - before, len(stores))
    return sorted(stores)


def discover_city_pages(f: Fetcher, cities: list[str]) -> list[str]:
    found: set = set()
    for i, cu in enumerate(cities, 1):
        html = f.get(cu)
        if html:
            found.update(city_page_store_links(html, cu))
        if i % 100 == 0:
            log.info("city pages %d/%d, store URLs found %d", i, len(cities), len(found))
    return sorted(found)


def discover_sitemap(f: Fetcher) -> tuple[set, list[str]]:
    """(store URLs, city-page URLs) from the sitemap (follows sitemap indexes)."""
    queue, seen, stores, cities = [SITEMAP], set(), set(), set()
    while queue and len(seen) < MAX_SITEMAPS:
        sm = queue.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        body = f.get_bytes(sm)
        if body is None:
            log.warning("sitemap fetch failed: %s", sm)
            continue
        locs, is_index = sitemap_locs(body)
        for u in locs:
            if is_index or u.lower().split("?")[0].endswith((".xml", ".xml.gz")):
                queue.append(u)
            elif store_key(u):
                stores.add(norm_store_url(u))
            elif is_city_page(u):
                cities.add(norm_store_url(u))
        log.info("sitemap %s: %d locs (index=%s), store URLs %d, city pages %d",
                 sm, len(locs), is_index, len(stores), len(cities))
    return stores, sorted(cities)


def main() -> int:
    setup_logging()
    t0 = time.monotonic()
    budget = float(os.environ.get("MAX_MINUTES", "270")) * 60
    f = Fetcher(use_playwright=False)

    urls = discover(f)
    print(f"store URLs discovered: {len(urls)}")
    if not urls:
        log.error("no /locations/{state}/{city}/{digits}/ URLs found via %s or city pages", SITEMAP)
        return 1

    if os.environ.get("FRESH") == "1" and OUT.exists():
        OUT.unlink()
    done = {r["store_url"] for r in read_csv(OUT)} if OUT.exists() else set()
    todo = [u for u in urls if u not in done]
    log.info("already crawled: %d, to fetch: %d (~%.0f min)", len(done & set(urls)), len(todo),
             len(todo) * 1.3 / 60)

    st = Counter()
    truncated = False
    for i, u in enumerate(todo, 1):
        if time.monotonic() - t0 > budget:
            truncated = True
            log.warning("time budget hit after %d/%d pages; re-run to continue", i - 1, len(todo))
            break
        html = f.get(u, has_data=take5_has_data)
        if html is None:
            st["fetch_error"] += 1
            continue
        rec = parse_take5_store(html, u)
        if rec["lat"] is None and rec["google_rating"] is None and not rec["street"]:
            st["parse_empty"] += 1
        rec["crawl_ts"] = ts_utc()
        append_rows(OUT, FIELDS, [rec])
        st["ok"] += 1
        if i % 100 == 0:
            log.info("pages %d/%d %s", i, len(todo), dict(st))

    # final: dedupe + sort (keeps latest crawl per store)
    rows = read_csv(OUT) if OUT.exists() else []
    latest = {r["store_url"]: r for r in rows}
    rows = sorted(latest.values(), key=lambda r: (r["state"], r["city"], r["store_number"]))
    write_csv(OUT, FIELDS, rows)

    n = len(rows)
    pct = lambda k: 100 * sum(1 for r in rows if r[k] not in ("", None)) / n if n else 0.0  # noqa: E731
    print("\n=== take5 crawl summary ===")
    print(f"stores discovered: {len(urls)}  rows in take5_stores.csv: {n}  states: "
          f"{len({r['state'] for r in rows})}  unmapped state slugs: "
          f"{sorted({r['state'] for r in rows if not r.get('state_abbr')})}")
    print(f"with lat/lon: {pct('lat'):.1f}%  with rating: {pct('google_rating'):.1f}%  "
          f"with street: {pct('street'):.1f}%  with coupon text: {pct('coupon_text'):.1f}%")
    print(f"this run: {dict(st)}  errors: {st['fetch_error']}  requests: {f.stats}  "
          f"elapsed: {(time.monotonic() - t0) / 60:.1f} min"
          + ("  [TRUNCATED — re-run to finish]" if truncated else ""))
    if n == 0 or (pct("lat") == 0 and pct("google_rating") == 0):
        log.error("no usable store data parsed — pages may be JS-rendered; check with SAVE_RAW=1")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
