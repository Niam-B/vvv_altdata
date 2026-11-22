"""Median household income by ZCTA (ZIP) from the Census ACS 5-year API (free, no key).

Writes data/ref/zcta_income.csv (zcta, median_hh_income, acs_year). One request.
Skips if the file already exists (use --force to refresh).
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "ref" / "zcta_income.csv"
YEARS = (2023, 2022, 2021)


def main() -> int:
    if OUT.exists() and "--force" not in sys.argv:
        print(f"{OUT} exists; skipping")
        return 0
    for y in YEARS:
        url = (f"https://api.census.gov/data/{y}/acs/acs5?get=B19013_001E"
               f"&for=zip%20code%20tabulation%20area:*")
        body = b""
        try:
            with urlopen(Request(url, headers={"User-Agent": "vvv-altdata research"}), timeout=120) as r:
                body = r.read()
            rows = json.loads(body)
        except Exception as e:  # try an older vintage
            print(f"ACS {y} failed: {e}; response starts: {body[:300]!r}")
            continue
        hdr = rows[0]
        i_inc, i_z = hdr.index("B19013_001E"), hdr.index("zip code tabulation area")
        OUT.parent.mkdir(parents=True, exist_ok=True)
        n = 0
        with OUT.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["zcta", "median_hh_income", "acs_year"])
            for r in rows[1:]:
                v = r[i_inc]
                if v is None or int(v) < 0:  # Census sentinels like -666666666
                    continue
                w.writerow([r[i_z], v, y])
                n += 1
        print(f"ACS {y}: {n} ZCTAs -> {OUT}")
        return 0
    print("could not fetch ACS income; income terciles will be empty")
    return 1


if __name__ == "__main__":
    sys.exit(main())
