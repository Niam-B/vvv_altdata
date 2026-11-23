"""Master crawl: index -> states -> cities -> every store page.

Writes data/ref/cities.csv and data/ref/stores.csv. Run occasionally (manually
via the master-crawl workflow); the daily collectors read these files.

Env:
  MAX_MINUTES  stop crawling store pages after this many minutes (default 270)
  STATES       comma-separated state codes to limit the crawl (testing)
  SAVE_RAW=1   save fetched HTML under data/raw/ (gitignored)
"""
# --- bootstrap: collectors/http.py would shadow stdlib `http`; import as a package
import sys, pathlib  # noqa: E401
_HERE = pathlib.Path(__file__).resolve().parent
sys.path[:] = [p for p in sys.path if pathlib.Path(p or ".").resolve() != _HERE]
sys.path.insert(0, str(_HERE.parent))
# ---------------------------------------------------------------------------

import json
import logging
import os
import time
from collections import Counter

from collectors.common import (
    ALIAS_FIELDS, ALIASES_CSV, BASE, REF, dedupe_stores, load_aliases, setup_logging, stratified_sample,
    ts_utc, write_csv,
)
from collectors.http import Fetcher
from collectors.parse import (
    city_has_data,
    parse_city,
    parse_index,
    parse_state,
    parse_store,
    path_parts,
    store_has_data,
)

log = logging.getLogger("master")

CITY_FIELDS = ["state", "city", "city_url"]
STORE_FIELDS = ["store_url", "name", "address", "state", "city", "lat", "lon", "place_id",
                "rating", "review_count", "coupons_json", "crawl_ts"]


def ensure_samples(cities: list[dict], stores: list[dict]) -> None:
    """Create the fixed wait/coupon samples once (committed with data/ref; never overwritten)."""
    from collections import Counter as _C
    from collectors.wait_times import SAMPLE_FIELDS as WS_FIELDS, SEC_PER_PAGE, build_sample
    from collectors.coupons_daily import SAMPLE_FIELDS as CS_FIELDS

    wpath, cpath = REF / "wait_sample.csv", REF / "coupon_sample.csv"
    if not wpath.exists() and len(cities) * SEC_PER_PAGE / 60 > float(os.environ.get("WAIT_MAX_MINUTES", "20")):
        counts = _C(s["store_url"].rsplit("/", 1)[0] for s in stores)
        n = write_csv(wpath, WS_FIELDS, build_sample(cities, counts, int(os.environ.get("WAIT_SAMPLE_STORES", "600"))))
        log.info("wait sample: %d city pages -> %s", n, wpath)
    if not cpath.exists() and stores:
        rows = [{k: s[k] for k in CS_FIELDS} for s in stores]
        sample = sorted(stratified_sample(rows, "state", int(os.environ.get("COUPON_SAMPLE_STORES", "400"))),
                        key=lambda r: r["store_url"])
        log.info("coupon sample: %d stores -> %s", write_csv(cpath, CS_FIELDS, sample), cpath)


def main() -> int:
    setup_logging()
    t0 = time.monotonic()
    budget = float(os.environ.get("MAX_MINUTES", "270")) * 60
    only = {s.strip().lower() for s in os.environ.get("STATES", "").split(",") if s.strip()}
    f = Fetcher()

    # 1. index -> states
    states: list[str] = []
    for url in (BASE + "/", BASE + "/index.html"):
        html = f.get(url, has_data=lambda h: bool(parse_index(h, url)))
        if html and (states := parse_index(html, url)):
            break
    if only:
        states = [s for s in states if path_parts(s)[0].lower() in only]
    log.info("states: %d", len(states))
    if not states:
        log.error("no state links found on index page — parser needs updating (run with SAVE_RAW=1)")
        return 1

    # 2. states -> cities (and stores linked directly from a state page)
    cities: dict[str, dict] = {}
    for su in states:
        st = path_parts(su)[0].lower()
        html = f.get(su, has_data=lambda h, su=su, st=st: any(parse_state(h, su, st)))
        if not html:
            continue
        cs, direct = parse_state(html, su, st)
        for cu in cs + ["/".join(d.split("/")[:-1]) for d in direct]:
            p = path_parts(cu)
            cities[cu] = {"state": st.upper(), "city": p[1], "city_url": cu}
        log.info("%s: %d cities", st.upper(), len(cs))
    write_csv(REF / "cities.csv", CITY_FIELDS, sorted(cities.values(), key=lambda r: r["city_url"]))
    log.info("cities: %d -> data/ref/cities.csv", len(cities))

    # 3. cities -> store listings
    listing: dict[str, dict] = {}
    for i, cu in enumerate(sorted(cities), 1):
        html = f.get(cu, has_data=city_has_data)
        if not html:
            continue
        rows = parse_city(html, cu)
        if not rows:
            log.warning("no stores parsed on %s", cu)
        for r in rows:
            listing.setdefault(r["store_url"], r)
        if i % 100 == 0:
            log.info("cities %d/%d, stores so far %d", i, len(cities), len(listing))
    log.info("store URLs found: %d", len(listing))

    # 4. store pages
    stores: list[dict] = []
    truncated = False
    for i, (su, lr) in enumerate(sorted(listing.items()), 1):
        if time.monotonic() - t0 > budget:
            log.warning("time budget hit after %d/%d store pages; remaining stores get listing data only",
                        i - 1, len(listing))
            truncated = True
        rec: dict = {}
        if not truncated:
            html = f.get(su, has_data=store_has_data)
            rec = parse_store(html, su) if html else {}
        p = path_parts(su)
        stores.append({
            "store_url": su,
            "name": rec.get("name") or lr.get("name", ""),
            "address": rec.get("address") or lr.get("address", ""),
            "state": p[0].upper(),
            "city": p[1],
            "lat": rec.get("lat"),
            "lon": rec.get("lon"),
            "place_id": rec.get("place_id", ""),
            "rating": rec.get("rating"),
            "review_count": rec.get("review_count"),
            "coupons_json": json.dumps(rec.get("coupons", []), separators=(",", ":")),
            "crawl_ts": ts_utc(),
        })
        if i % 100 == 0:
            log.info("store pages %d/%d", i, len(listing))
    n_raw = len(stores)
    stores, alias = dedupe_stores(stores)  # same store under two URLs (e.g. trailing period)
    aliases = {**load_aliases(), **{a: c for a, c in alias.items() if a != c}}
    write_csv(REF / ALIASES_CSV, ALIAS_FIELDS,
              [{"alias_url": a, "store_url": c} for a, c in sorted(aliases.items())])
    log.info("de-duplicated stores: %d -> %d", n_raw, len(stores))
    write_csv(REF / "stores.csv", STORE_FIELDS, stores)
    ensure_samples(list(cities.values()), stores)

    # 5. summary
    per_state = Counter(s["state"] for s in stores)
    with_pid = sum(1 for s in stores if s["place_id"])
    with_geo = sum(1 for s in stores if s["lat"] is not None)
    with_rating = sum(1 for s in stores if s["rating"] is not None)
    print("\n=== master crawl summary ===")
    for st, n in sorted(per_state.items()):
        print(f"  {st}: {n}")
    total = len(stores)
    pct = lambda n: f"{100 * n / total:.1f}%" if total else "n/a"  # noqa: E731
    print(f"TOTAL stores: {total}  cities: {len(cities)}  states: {len(per_state)}")
    print(f"with place_id: {with_pid} ({pct(with_pid)})  with lat/lon: {pct(with_geo)}  "
          f"with rating: {pct(with_rating)}")
    print(f"requests: {f.stats}  elapsed: {(time.monotonic() - t0) / 60:.1f} min"
          + ("  [TRUNCATED by MAX_MINUTES]" if truncated else ""))
    return 0 if total else 1


if __name__ == "__main__":
    sys.exit(main())
