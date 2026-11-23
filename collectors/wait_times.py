"""Live wait-time snapshot from city pages.

Reads data/ref/cities.csv (+ stores.csv for per-city store counts). If a full
pass over all city pages would exceed WAIT_MAX_MINUTES (default 20), uses a
fixed state-stratified sample of city pages covering ~WAIT_SAMPLE_STORES (600)
stores, saved once to data/ref/wait_sample.csv and reused on every run.

Appends (ts_utc, store_url, wait_bucket, status) to
data/live/wait_times/YYYY-MM-DD.csv. status is one of:
  ok           wait bucket shown
  no_wait      store listed but no wait shown
  closed       store listed, no wait shown, hours text says closed
  missing      store in stores.csv for this city but not on the page
  fetch_error  city page could not be fetched
"""
# --- bootstrap: collectors/http.py would shadow stdlib `http`; import as a package
import sys, pathlib  # noqa: E401
_HERE = pathlib.Path(__file__).resolve().parent
sys.path[:] = [p for p in sys.path if pathlib.Path(p or ".").resolve() != _HERE]
sys.path.insert(0, str(_HERE.parent))
# ---------------------------------------------------------------------------

import logging
import os
import random
import time
from collections import Counter, defaultdict

from collectors.common import (
    REF, SAMPLE_SEED, append_rows, canonical_url, daily_csv, load_stores, read_csv, setup_logging,
    store_url_key, ts_utc, write_csv,
)
from collectors.http import Fetcher
from collectors.parse import city_has_data, parse_city

log = logging.getLogger("wait_times")

FIELDS = ["ts_utc", "store_url", "wait_bucket", "status"]
SAMPLE_FIELDS = ["city_url", "state", "city", "n_stores"]
SEC_PER_PAGE = 1.8  # 1.0s min interval + avg 0.25s jitter + ~0.5s response


def build_sample(cities: list[dict], counts: Counter, target: int) -> list[dict]:
    by_state: dict[str, list[dict]] = defaultdict(list)
    for c in cities:
        by_state[c["state"]].append({**c, "n_stores": counts.get(c["city_url"], 1)})
    total = sum(c["n_stores"] for g in by_state.values() for c in g)
    rng = random.Random(SAMPLE_SEED)
    sample = []
    for st in sorted(by_state):
        group = sorted(by_state[st], key=lambda c: c["city_url"])
        rng.shuffle(group)
        quota = max(1, round(target * sum(c["n_stores"] for c in group) / total))
        got = 0
        for c in group:
            if got >= quota:
                break
            sample.append(c)
            got += c["n_stores"]
    return sorted(sample, key=lambda c: c["city_url"])


def city_rows(parsed: list[dict], known: set, alias: dict, now: str) -> list[dict]:
    """Output rows for one city page: one per store (duplicate URLs collapsed to the canonical
    store), plus a 'missing' row for each known store of this city not on the page."""
    rows, seen, seen_keys = [], set(), set()
    for r in parsed:
        url = canonical_url(r["store_url"], alias)
        if store_url_key(url) in seen_keys:  # same store listed twice (e.g. trailing-period URL)
            continue
        seen_keys.add(store_url_key(url))
        seen.add(url)
        if r["wait_bucket"]:
            status = "ok"
        elif "closed" in r["hours_text"].lower():
            status = "closed"
        else:
            status = "no_wait"
        rows.append({"ts_utc": now, "store_url": url, "wait_bucket": r["wait_bucket"], "status": status})
    for s in sorted(known - seen):
        rows.append({"ts_utc": now, "store_url": s, "wait_bucket": "", "status": "missing"})
    return rows


def main() -> int:
    setup_logging()
    t0 = time.monotonic()
    max_min = float(os.environ.get("WAIT_MAX_MINUTES", "20"))  # full-pass threshold
    cap_min = float(os.environ.get("WAIT_RUNTIME_CAP_MINUTES", "19"))  # hard stop (job timeout is 25)
    target = int(os.environ.get("WAIT_SAMPLE_STORES", "600"))

    cities = read_csv(REF / "cities.csv")
    # de-duplicated: the same store can be listed under two URLs (see common.dedupe_stores)
    stores, alias = load_stores() if (REF / "stores.csv").exists() else ([], {})
    city_of = lambda u: u.rsplit("/", 1)[0]  # noqa: E731
    stores_by_city: dict[str, set] = defaultdict(set)
    for s in stores:
        stores_by_city[city_of(s["store_url"])].add(s["store_url"])
    counts = Counter({c: len(v) for c, v in stores_by_city.items()})

    est_min = len(cities) * SEC_PER_PAGE / 60
    sample_path = REF / "wait_sample.csv"
    if est_min <= max_min:
        targets = cities
        log.info("full pass: %d city pages (~%.1f min)", len(targets), est_min)
    else:
        if sample_path.exists():
            targets = read_csv(sample_path)
        else:
            targets = build_sample(cities, counts, target)
            write_csv(sample_path, SAMPLE_FIELDS, targets)
            log.info("created fixed sample -> %s", sample_path)
        log.info("full pass would take ~%.0f min; using sample of %d city pages (~%d stores)",
                 est_min, len(targets), sum(int(t.get("n_stores") or 1) for t in targets))

    f = Fetcher()
    out_path = daily_csv("wait_times")
    status_ct: Counter = Counter()
    n_rows = 0
    for i, c in enumerate(targets):
        if time.monotonic() - t0 > cap_min * 60:
            log.warning("time budget hit: %d/%d city pages done", i, len(targets))
            break
        cu = c["city_url"]
        html = f.get(cu, has_data=city_has_data)
        now = ts_utc()
        rows = []
        if html is None:
            rows = [{"ts_utc": now, "store_url": s, "wait_bucket": "", "status": "fetch_error"}
                    for s in sorted(stores_by_city.get(cu, ()))] or \
                   [{"ts_utc": now, "store_url": cu, "wait_bucket": "", "status": "fetch_error"}]
        else:
            rows = city_rows(parse_city(html, cu), stores_by_city.get(cu, set()), alias, now)
        status_ct.update(r["status"] for r in rows)
        n_rows += append_rows(out_path, FIELDS, rows)

    print(f"\n=== wait_times: {n_rows} rows -> {out_path.relative_to(REF.parents[1])} ===")
    print(f"status: {dict(status_ct)}")
    print(f"requests: {f.stats}  elapsed: {(time.monotonic() - t0) / 60:.1f} min")
    return 0 if n_rows else 1


if __name__ == "__main__":
    sys.exit(main())
