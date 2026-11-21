"""Daily coupons / rating snapshot for a fixed 400-store stratified sample.

The sample (stratified by state, fixed seed) is created once from
data/ref/stores.csv and saved to data/ref/coupon_sample.csv; every run reuses it.

Appends (ts_utc, store_url, rating, review_count, coupons_json) to
data/live/coupons/YYYY-MM-DD.csv.
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

from collectors.common import (
    REF, append_rows, daily_csv, read_csv, setup_logging, stratified_sample, ts_utc, write_csv,
)
from collectors.http import Fetcher
from collectors.parse import parse_store, store_has_data

log = logging.getLogger("coupons")

FIELDS = ["ts_utc", "store_url", "rating", "review_count", "coupons_json"]
SAMPLE_FIELDS = ["store_url", "state", "city"]


def main() -> int:
    setup_logging()
    t0 = time.monotonic()
    n = int(os.environ.get("COUPON_SAMPLE_STORES", "400"))
    max_min = float(os.environ.get("COUPON_MAX_MINUTES", "13"))

    sample_path = REF / "coupon_sample.csv"
    if sample_path.exists():
        sample = read_csv(sample_path)
    else:
        stores = [{k: s[k] for k in SAMPLE_FIELDS} for s in read_csv(REF / "stores.csv")]
        sample = sorted(stratified_sample(stores, "state", n), key=lambda r: r["store_url"])
        write_csv(sample_path, SAMPLE_FIELDS, sample)
        log.info("created fixed %d-store sample -> %s", len(sample), sample_path)

    f = Fetcher()
    out_path = daily_csv("coupons")
    ok = n_coupons = 0
    rows_written = 0
    for i, s in enumerate(sample):
        if time.monotonic() - t0 > max_min * 60:
            log.warning("time budget hit: %d/%d stores done", i, len(sample))
            break
        html = f.get(s["store_url"], has_data=store_has_data)
        if html is None:
            row = {"ts_utc": ts_utc(), "store_url": s["store_url"], "rating": "", "review_count": "",
                   "coupons_json": ""}
        else:
            rec = parse_store(html, s["store_url"])
            ok += 1
            n_coupons += len(rec["coupons"])
            row = {"ts_utc": ts_utc(), "store_url": s["store_url"], "rating": rec["rating"],
                   "review_count": rec["review_count"],
                   "coupons_json": json.dumps(rec["coupons"], separators=(",", ":"))}
        rows_written += append_rows(out_path, FIELDS, [row])

    print(f"\n=== coupons: {rows_written} rows ({ok} fetched OK, {n_coupons} coupons) -> {out_path} ===")
    print(f"requests: {f.stats}  elapsed: {(time.monotonic() - t0) / 60:.1f} min")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
