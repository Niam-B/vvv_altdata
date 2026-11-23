"""Median household income by ZCTA (ZIP) from Census ACS 5-year table B19013 (free, no key).

Writes data/ref/zcta_income.csv (zcta, median_hh_income, acs_year).

Sources, tried in order for each vintage (newest first):
  1. Census Data API   api.census.gov/data/{y}/acs/acs5?get=B19013_001E&for=zip code tabulation area:*
  2. ACS table-based Summary File (bulk, pipe-delimited) on www2.census.gov:
     .../summary_file/{y}/table-based-SF/data/5YRData/acsdt5y{y}-b19013.dat
Fails loudly (exit 1, with the start of each bad response) if nothing usable is fetched or
fewer than MIN_ZCTAS rows are parsed. Skips if the file exists unless --force.
"""
from __future__ import annotations

import csv
import io
import json
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "ref" / "zcta_income.csv"
YEARS = (2023, 2022)
MIN_ZCTAS = 25_000  # there are ~33,000 ZCTAs; fewer means a bad parse
UA = {"User-Agent": "Mozilla/5.0 (vvv-altdata research; python-requests)"}


def _valid(v) -> int | None:
    try:
        x = int(float(v))
    except (TypeError, ValueError):
        return None
    return x if x > 0 else None  # Census sentinels (-666666666 etc.) are negative


def from_api(y: int) -> dict[str, int]:
    url = (f"https://api.census.gov/data/{y}/acs/acs5?get=B19013_001E"
           f"&for=zip%20code%20tabulation%20area:*")
    r = requests.get(url, headers=UA, timeout=180)
    print(f"  API {y}: HTTP {r.status_code}, {len(r.content):,} bytes, content-type "
          f"{r.headers.get('content-type')!r}")
    r.raise_for_status()
    try:
        rows = json.loads(r.text)
    except ValueError:
        raise RuntimeError(f"non-JSON response, starts: {r.text[:300]!r}") from None
    hdr = rows[0]
    i_inc, i_z = hdr.index("B19013_001E"), hdr.index("zip code tabulation area")
    return {r[i_z]: v for r in rows[1:] if (v := _valid(r[i_inc])) is not None}


def from_summary_file(y: int) -> dict[str, int]:
    url = (f"https://www2.census.gov/programs-surveys/acs/summary_file/{y}/table-based-SF/data/"
           f"5YRData/acsdt5y{y}-b19013.dat")
    r = requests.get(url, headers=UA, timeout=600)
    print(f"  summary file {y}: HTTP {r.status_code}, {len(r.content):,} bytes")
    r.raise_for_status()
    rd = csv.reader(io.StringIO(r.text), delimiter="|")
    hdr = next(rd)
    i_geo = hdr.index("GEO_ID")
    i_inc = next(i for i, h in enumerate(hdr) if h.upper() in ("B19013_E001", "B19013_001E"))
    out = {}
    for row in rd:
        geo = row[i_geo]
        if geo.startswith("860Z200US") and (v := _valid(row[i_inc])) is not None:  # ZCTA level
            out[geo[-5:]] = v
    return out


def main() -> int:
    if OUT.exists() and "--force" not in sys.argv:
        print(f"{OUT} exists; skipping (use --force to refresh)")
        return 0
    errors = []
    for y in YEARS:
        for name, fn in (("api", from_api), ("summary_file", from_summary_file)):
            try:
                data = fn(y)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{name} {y}: {e}")
                print(f"  FAILED {name} {y}: {e}")
                continue
            if len(data) < MIN_ZCTAS:
                errors.append(f"{name} {y}: only {len(data)} ZCTAs parsed")
                print(f"  FAILED {name} {y}: only {len(data)} ZCTAs parsed")
                continue
            OUT.parent.mkdir(parents=True, exist_ok=True)
            with OUT.open("w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["zcta", "median_hh_income", "acs_year"])
                for z in sorted(data):
                    w.writerow([z, data[z], y])
            print(f"ACS {y} via {name}: {len(data):,} ZCTAs -> {OUT.relative_to(ROOT)}")
            return 0
    print("ERROR: could not fetch ACS B19013 by ZCTA from any source:\n  " + "\n  ".join(errors))
    return 1


if __name__ == "__main__":
    sys.exit(main())
