"""Discover pre-late-2025 VIOC store-page URL patterns from the Wayback CDX index (free).

For each candidate host, lists every archived HTML URL (200 status) captured before
2025-10-01 (one row per URL = its earliest capture) and writes
data/hist/old_url_candidates.csv (host, original, path, first_ts). Prints per-host counts,
the most common path shapes, and examples, so the old store-page pattern can be identified.

    python scripts/probe_old_vioc_urls.py [--to 20250930]
"""
import argparse
import csv
import json
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import quote, urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "hist" / "old_url_candidates.csv"
CDX = "https://web.archive.org/cdx/search/cdx"
HOSTS = ["vioc.com", "www.vioc.com", "store.vioc.com", "stores.vioc.com", "locations.vioc.com",
         "local.vioc.com", "www.valvoline.com", "valvolineinstantoilchange.com",
         "www.valvolineinstantoilchange.com"]
UA = {"User-Agent": "vvv-altdata research (wayback cdx)"}


def cdx(params: dict, tries: int = 5):
    for i in range(tries):
        try:
            r = requests.get(CDX, params=params, headers=UA, timeout=300)
            if r.status_code == 200:
                return json.loads(r.text) if r.text.strip() else []
            print(f"  CDX HTTP {r.status_code}: {r.text[:200]!r}")
        except Exception as e:  # noqa: BLE001
            print(f"  CDX error: {e}")
        time.sleep(5 * (i + 1))
    return None


def shape(path: str) -> str:
    segs = [s for s in path.split("/") if s]
    out = []
    for i, s in enumerate(segs[:5]):
        if re.fullmatch(r"\d+", s):
            out.append("{num}")
        elif i == 0:
            out.append(s.lower())
        elif re.fullmatch(r"[a-z]{2}", s, re.I):
            out.append("{st}")
        elif re.match(r"\d+[-_]", s):
            out.append("{addr}")
        else:
            out.append("{slug}")
    return "/" + "/".join(out) + ("/…" if len(segs) > 5 else "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", default="20250930")
    args = ap.parse_args()
    rows = []
    for host in HOSTS:
        t0 = time.time()
        data = cdx({"url": host + "/", "matchType": "host", "output": "json", "fl": "original,timestamp",
                    "filter": ["statuscode:200", "mimetype:text/html"], "collapse": "urlkey",
                    "to": args.to, "limit": "500000"})
        if data is None:
            print(f"{host}: CDX failed")
            continue
        recs = data[1:] if data and data[0] == ["original", "timestamp"] else data
        for orig, ts in recs:
            p = urlparse(orig)
            rows.append({"host": host, "original": orig, "path": p.path, "first_ts": ts})
        print(f"\n=== {host}: {len(recs)} archived HTML URLs before {args.to} ({time.time() - t0:.0f}s)")
        shapes = Counter()
        ex = defaultdict(list)
        years = Counter()
        for orig, ts in recs:
            sh = shape(urlparse(orig).path)
            shapes[sh] += 1
            years[ts[:4]] += 1
            if len(ex[sh]) < 4:
                ex[sh].append(f"{ts[:8]} {orig}")
        print("  first-capture years:", dict(sorted(years.items())))
        for sh, n in shapes.most_common(15):
            print(f"  {n:7d}  {sh}")
            for e in ex[sh]:
                print(f"             {e}")
        time.sleep(2)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["host", "original", "path", "first_ts"])
        w.writeheader()
        w.writerows(rows)
    print(f"\n-> {OUT.relative_to(ROOT)} ({len(rows)} rows)")
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())
