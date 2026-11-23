"""Take 5 competitor density around every Valvoline store.

Inputs : data/ref/stores.csv (Valvoline), data/ref/take5_stores.csv
         data/ref/zcta_income.csv (optional; written by scripts/fetch_acs_income.py)
Output : data/derived/vvv_competition.csv (one row per Valvoline store)
Exit 1 if an input is missing or has no stores with coordinates.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from collectors.common import dedupe_stores  # noqa: E402

REF = ROOT / "data" / "ref"
DERIVED = ROOT / "data" / "derived"
EARTH_MI = 3958.7613
RADII = (1, 3, 5)


def haversine_matrix(lat1, lon1, lat2, lon2) -> np.ndarray:
    """Miles between every point in set 1 (rows) and set 2 (cols)."""
    p1, l1 = np.radians(np.asarray(lat1, float))[:, None], np.radians(np.asarray(lon1, float))[:, None]
    p2, l2 = np.radians(np.asarray(lat2, float))[None, :], np.radians(np.asarray(lon2, float))[None, :]
    a = np.sin((p2 - p1) / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin((l2 - l1) / 2) ** 2
    return 2 * EARTH_MI * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def compute(vvv: pd.DataFrame, t5: pd.DataFrame) -> pd.DataFrame:
    out = vvv.copy()
    for c in ("lat", "lon"):
        out[c] = pd.to_numeric(out[c], errors="coerce")
        t5[c] = pd.to_numeric(t5[c], errors="coerce")
    has = out["lat"].notna() & out["lon"].notna()
    t5g = t5.dropna(subset=["lat", "lon"]).reset_index(drop=True)
    v = out[has]

    for r in RADII:
        out[f"t5_within_{r}mi"] = pd.NA
    out["nearest_t5_miles"] = np.nan
    out["nearest_t5_url"] = ""
    out["vvv_within_3mi"] = pd.NA

    if len(v) and len(t5g):
        d = haversine_matrix(v["lat"], v["lon"], t5g["lat"], t5g["lon"])
        for r in RADII:
            out.loc[has, f"t5_within_{r}mi"] = (d <= r).sum(axis=1)
        idx = d.argmin(axis=1)
        out.loc[has, "nearest_t5_miles"] = d[np.arange(len(v)), idx].round(3)
        out.loc[has, "nearest_t5_url"] = t5g.loc[idx, "store_url"].to_numpy()
    if len(v):
        dv = haversine_matrix(v["lat"], v["lon"], v["lat"], v["lon"])
        np.fill_diagonal(dv, np.inf)  # exclude itself
        out.loc[has, "vvv_within_3mi"] = (dv <= 3).sum(axis=1)
    for c in [f"t5_within_{r}mi" for r in RADII] + ["vvv_within_3mi"]:
        out[c] = out[c].astype("Int64")
    return out


def offer_amounts(coupon_text: pd.Series) -> pd.Series:
    """Stores per offer amount ('$15 off', '20% off', ...): each amount counted once per store."""
    amts = coupon_text.fillna("").map(
        lambda s: sorted({re.sub(r"\s+", " ", m.lower()).replace("$ ", "$")
                          for m in re.findall(r"\$\s?\d+(?:\.\d{2})?\s*off|\d{1,2}\s?%\s*off", s, re.I)}))
    return amts.explode().dropna().value_counts()


def main() -> int:
    vpath, tpath = REF / "stores.csv", REF / "take5_stores.csv"
    for p in (vpath, tpath):
        if not p.exists():
            print(f"ERROR: missing input {p.relative_to(ROOT)}")
            return 1
    vvv = pd.read_csv(vpath, dtype=str, keep_default_na=False)
    n_raw = len(vvv)
    # the same store can appear under two URLs; count each store once (incl. vvv_within_3mi)
    vvv = pd.DataFrame(dedupe_stores(vvv.to_dict("records"))[0])
    for c in ("lat", "lon", "rating", "review_count"):
        vvv[c] = pd.to_numeric(vvv[c], errors="coerce")
    if len(vvv) != n_raw:
        print(f"de-duplicated Valvoline stores: {n_raw} -> {len(vvv)}")
    t5 = pd.read_csv(tpath, dtype={"zip": str, "store_number": str})
    if vvv[["lat", "lon"]].dropna().empty or t5[["lat", "lon"]].dropna().empty:
        print("ERROR: Valvoline or Take 5 stores have no coordinates")
        return 1

    vvv["zip"] = vvv["address"].fillna("").str.findall(r"\b(\d{5})(?:-\d{4})?\b").str[-1]
    out = compute(vvv, t5)

    inc_path = next((p for p in (REF / "zcta_income.csv", REF / "acs_income.csv") if p.exists()), None)
    if inc_path:
        inc = pd.read_csv(inc_path, dtype={"zcta": str})
        out = out.merge(inc[["zcta", "median_hh_income"]], left_on="zip", right_on="zcta", how="left") \
                 .drop(columns="zcta")
    else:
        out["median_hh_income"] = np.nan

    cols = ["store_url", "name", "state", "city", "zip", "lat", "lon", "rating", "review_count",
            "t5_within_1mi", "t5_within_3mi", "t5_within_5mi", "nearest_t5_miles", "nearest_t5_url",
            "vvv_within_3mi", "median_hh_income"]
    DERIVED.mkdir(parents=True, exist_ok=True)
    dest = DERIVED / "vvv_competition.csv"
    out[cols].to_csv(dest, index=False)

    # ---- summary
    g = out[out["t5_within_3mi"].notna()]
    n = len(g)
    print("=== competition summary ===")
    print(f"Valvoline stores: {len(out)} ({n} with coordinates)   Take 5 stores: {len(t5)} "
          f"({t5['lat'].notna().sum()} with coordinates)")
    print(f"income joined: {out['median_hh_income'].notna().sum()} stores"
          + ("" if inc_path else " (no zcta_income.csv — run scripts/fetch_acs_income.py)"))
    bins = pd.cut(g["t5_within_3mi"].astype(int), [-1, 0, 1, 2, 3, 5, 10, 10_000],
                  labels=["0", "1", "2", "3", "4-5", "6-10", "11+"])
    dist = bins.value_counts().sort_index()
    print("\nTake 5 stores within 3 mi of a Valvoline store (distribution):")
    for k, c in dist.items():
        print(f"  {k:>5}: {c:5d}  ({100 * c / n:5.1f}%)")
    print(f"\nshare of Valvoline stores with a Take 5 within 1 mi: "
          f"{100 * (g['t5_within_1mi'] > 0).mean():.1f}%   within 3 mi: {100 * (g['t5_within_3mi'] > 0).mean():.1f}%"
          f"   within 5 mi: {100 * (g['t5_within_5mi'] > 0).mean():.1f}%")
    print(f"median distance to nearest Take 5: {g['nearest_t5_miles'].median():.2f} mi;  "
          f"median other Valvoline within 3 mi: {g['vvv_within_3mi'].median():.0f}")
    print("\n                 median rating   median review_count")
    print(f"  Take 5         {pd.to_numeric(t5['google_rating'], errors='coerce').median():13.2f}   "
          f"{pd.to_numeric(t5['review_count'], errors='coerce').median():19.0f}")
    print(f"  Valvoline      {pd.to_numeric(vvv['rating'], errors='coerce').median():13.2f}   "
          f"{pd.to_numeric(vvv['review_count'], errors='coerce').median():19.0f}")
    tally = offer_amounts(t5["coupon_text"]) if "coupon_text" in t5 else pd.Series(dtype=int)
    print(f"\nTake 5 coupon offers by amount (stores offering; {t5['coupon_text'].notna().sum()} stores "
          f"with any offer text):")
    for k, c in tally.head(15).items():
        print(f"  {k:>10}: {c}")
    print(f"\n-> {dest.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
