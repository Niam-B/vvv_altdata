"""Quarterly same-store Google review growth panel (proxy for transaction volume).

Inputs
  data/hist/wayback_snapshots.csv   archived review_count, one snapshot per store-quarter
  data/hist/wayback_cdx.csv         first Wayback capture per store (store-age proxy)
  data/live/coupons/*.csv           live daily review_count for the same sample stores
  data/ref/stores.csv               name / address (ZIP) / state
  data/ref/zcta_income.csv          ACS median household income by ZIP (optional)
  data/ref/breeze_stores.csv        optional manual list (store_url) of Breeze/Oil Changers stores

Method
  * One review_count observation per store-quarter: the latest snapshot in the quarter
    (Wayback or live; live wins when later).
  * new_reviews_91d = (count_t - count_{t-1}) / days_between * 91, attributed to the quarter
    of the later snapshot. Usable only if MIN_GAP <= gap <= MAX_GAP days and the count
    did not fall.
  * Same-store set for quarter q: stores usable in q and in q-4, excluding Breeze.
    YoY = sum(new_reviews_91d in q) / sum(in q-4) - 1, overall, by income tercile and by
    store-age bucket. Median store-level YoY is reported alongside.

Outputs
  data/hist/review_panel_quarterly.csv  store-quarter panel
  data/hist/review_yoy_summary.csv      quarter x group same-store YoY
Breeze/Oil Changers flag: name matches breeze|oil changers, OR store_url in
breeze_stores.csv, OR first Wayback capture between 2025-12-01 and 2026-02-28
(stores that appeared on store.vioc.com right after the Dec 1 2025 acquisition).
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
HIST = ROOT / "data" / "hist"
REF = ROOT / "data" / "ref"
LIVE = ROOT / "data" / "live"

MIN_GAP, MAX_GAP = 45, 183
BREEZE_RE = re.compile(r"breeze|oil changers", re.I)
BREEZE_WINDOW = ("2025-12-01", "2026-02-28")
AGE_BINS = [("<=2019 (established)", "0000", "2019"), ("2020-2022", "2020", "2022"),
            ("2023+ (newer)", "2023", "9999")]


def fiscal_quarter(q: pd.Period) -> str:
    """VVV FY ends Sep 30: Oct-Dec = FQ1 of next FY."""
    fy = q.year + 1 if q.quarter == 4 else q.year
    fq = 1 if q.quarter == 4 else q.quarter + 1
    return f"FY{fy}Q{fq}"


def load_observations(hist: Path, live: Path) -> pd.DataFrame:
    obs = []
    p = hist / "wayback_snapshots.csv"
    if p.exists():
        w = pd.read_csv(p, dtype=str)
        w = w[w["status"] == "ok"].copy()
        w["review_count"] = pd.to_numeric(w["review_count"], errors="coerce")
        w["date"] = pd.to_datetime(w["snapshot_date"])
        w["source"] = "wayback"
        obs.append(w[["store_url", "date", "review_count", "source"]])
    files = sorted((live / "coupons").glob("*.csv"))
    if files:
        lv = pd.concat([pd.read_csv(f, dtype=str) for f in files], ignore_index=True)
        lv["review_count"] = pd.to_numeric(lv["review_count"], errors="coerce")
        lv["date"] = pd.to_datetime(lv["ts_utc"].str[:10])
        lv["source"] = "live"
        obs.append(lv[["store_url", "date", "review_count", "source"]])
    if not obs:
        return pd.DataFrame(columns=["store_url", "date", "review_count", "source", "quarter"])
    df = pd.concat(obs, ignore_index=True).dropna(subset=["review_count"])
    df["quarter"] = df["date"].dt.to_period("Q")
    # one observation per store-quarter: the latest
    return (df.sort_values(["store_url", "date"]).groupby(["store_url", "quarter"], as_index=False)
              .last())


def build_panel(obs: pd.DataFrame) -> pd.DataFrame:
    obs = obs.sort_values(["store_url", "date"]).copy()
    g = obs.groupby("store_url")
    obs["prev_date"] = g["date"].shift()
    obs["prev_review_count"] = g["review_count"].shift()
    obs["gap_days"] = (obs["date"] - obs["prev_date"]).dt.days
    delta = obs["review_count"] - obs["prev_review_count"]
    obs["new_reviews_91d"] = delta / obs["gap_days"] * 91
    reason = np.select(
        [obs["prev_date"].isna(), obs["gap_days"] < MIN_GAP, obs["gap_days"] > MAX_GAP, delta < 0],
        ["first_obs", "gap_too_short", "gap_too_long", "count_fell"], default="")
    obs["unusable_reason"] = reason
    obs["usable"] = reason == ""
    return obs


def store_attributes(hist: Path, ref: Path) -> pd.DataFrame:
    stores = pd.read_csv(ref / "stores.csv", dtype=str) if (ref / "stores.csv").exists() \
        else pd.DataFrame(columns=["store_url", "name", "address", "state"])
    a = stores[["store_url", "name", "address", "state"]].copy()
    a["zip"] = a["address"].fillna("").str.findall(r"\b(\d{5})(?:-\d{4})?\b").str[-1]
    cdx_p = hist / "wayback_cdx.csv"
    if cdx_p.exists():
        cdx = pd.read_csv(cdx_p, dtype=str)
        cdx = cdx[cdx["cdx_status"] == "ok"].drop_duplicates("store_url", keep="last")
        a = a.merge(cdx[["store_url", "first_capture"]], on="store_url", how="left")
    else:
        a["first_capture"] = np.nan
    fc = a["first_capture"].fillna("")
    fc_date = fc.str[:4] + "-" + fc.str[4:6] + "-" + fc.str[6:8]
    a["age_bucket"] = ""
    for label, lo, hi in AGE_BINS:
        a.loc[(fc != "") & (fc.str[:4] >= lo) & (fc.str[:4] <= hi), "age_bucket"] = label
    manual = set()
    if (ref / "breeze_stores.csv").exists():
        manual = set(pd.read_csv(ref / "breeze_stores.csv", dtype=str)["store_url"])
    a["breeze"] = (a["name"].fillna("").str.contains(BREEZE_RE) | a["store_url"].isin(manual)
                   | ((fc != "") & (fc_date >= BREEZE_WINDOW[0]) & (fc_date <= BREEZE_WINDOW[1])))
    inc_p = ref / "zcta_income.csv"
    if inc_p.exists():
        inc = pd.read_csv(inc_p, dtype={"zcta": str})
        a = a.merge(inc[["zcta", "median_hh_income"]], left_on="zip", right_on="zcta", how="left") \
             .drop(columns="zcta")
    else:
        a["median_hh_income"] = np.nan
    return a


def assign_income_terciles(attrs: pd.DataFrame, panel_stores: set) -> pd.DataFrame:
    a = attrs.copy()
    a["income_tercile"] = ""
    m = a["store_url"].isin(panel_stores) & a["median_hh_income"].notna()
    if m.sum() >= 3:
        a.loc[m, "income_tercile"] = pd.qcut(a.loc[m, "median_hh_income"], 3,
                                             labels=["T1 low", "T2 mid", "T3 high"]).astype(str)
    return a


def same_store_yoy(panel: pd.DataFrame, group_col: str | None) -> pd.DataFrame:
    u = panel[panel["usable"] & ~panel["breeze"]]
    cur = u[["store_url", "quarter", "new_reviews_91d"] + ([group_col] if group_col else [])]
    prev = u[["store_url", "quarter", "new_reviews_91d"]].copy()
    prev["quarter"] = prev["quarter"] + 4
    m = cur.merge(prev, on=["store_url", "quarter"], suffixes=("", "_ya"))
    keys = ["quarter"] + ([group_col] if group_col else [])
    m["store_yoy"] = np.where(m["new_reviews_91d_ya"] > 0,
                              m["new_reviews_91d"] / m["new_reviews_91d_ya"] - 1, np.nan)
    out = m.groupby(keys).agg(n_stores=("store_url", "nunique"),
                              reviews_91d=("new_reviews_91d", "sum"),
                              reviews_91d_ya=("new_reviews_91d_ya", "sum"),
                              median_store_yoy=("store_yoy", "median")).reset_index()
    out["yoy_agg"] = out["reviews_91d"] / out["reviews_91d_ya"] - 1
    out["group_type"] = group_col or "all"
    out["group"] = out[group_col] if group_col else "all"
    return out.drop(columns=[group_col] if group_col else [])


def run(hist: Path = HIST, ref: Path = REF, live: Path = LIVE, quiet: bool = False) -> dict:
    obs = load_observations(hist, live)
    if obs.empty:
        print("no review_count observations yet (run the wayback-reviews workflow)")
        return {}
    panel = build_panel(obs)
    attrs = store_attributes(hist, ref)
    attrs = assign_income_terciles(attrs, set(panel["store_url"]))
    panel = panel.merge(attrs.drop(columns=["name", "address"]), on="store_url", how="left")
    panel["breeze"] = panel["breeze"].fillna(False).astype(bool)
    for c in ("income_tercile", "age_bucket"):
        panel[c] = panel[c].fillna("")
    panel["fiscal_quarter"] = panel["quarter"].map(fiscal_quarter)

    cols = ["store_url", "state", "quarter", "fiscal_quarter", "source", "date", "review_count",
            "prev_date", "prev_review_count", "gap_days", "new_reviews_91d", "usable",
            "unusable_reason", "breeze", "zip", "median_hh_income", "income_tercile",
            "first_capture", "age_bucket"]
    out = panel[cols].sort_values(["store_url", "quarter"]).copy()
    out["quarter"] = out["quarter"].astype(str)
    hist.mkdir(parents=True, exist_ok=True)
    out.to_csv(hist / "review_panel_quarterly.csv", index=False, float_format="%.4f")

    yoy = pd.concat([same_store_yoy(panel, None),
                     same_store_yoy(panel[panel["income_tercile"] != ""], "income_tercile"),
                     same_store_yoy(panel[panel["age_bucket"] != ""], "age_bucket")],
                    ignore_index=True)
    if not yoy.empty:
        yoy["fiscal_quarter"] = yoy["quarter"].map(fiscal_quarter)
        yoy["quarter"] = yoy["quarter"].astype(str)
        yoy = yoy[["quarter", "fiscal_quarter", "group_type", "group", "n_stores", "reviews_91d",
                   "reviews_91d_ya", "yoy_agg", "median_store_yoy"]]
    yoy.to_csv(hist / "review_yoy_summary.csv", index=False, float_format="%.4f")

    # coverage diagnostics
    n_stores = panel["store_url"].nunique()
    usable_q = panel[panel["usable"]].groupby("store_url")["quarter"].nunique()
    pct12 = 100 * (usable_q >= 12).sum() / n_stores if n_stores else 0.0
    med_gap = panel["gap_days"].median()
    med_gap_usable = panel.loc[panel["usable"], "gap_days"].median()
    stats = {"stores": n_stores, "pct_ge12_usable_quarters": pct12,
             "median_gap_days": med_gap, "median_gap_days_usable": med_gap_usable,
             "breeze_excluded": int(attrs.loc[attrs["store_url"].isin(panel["store_url"]), "breeze"].sum()),
             "usable_store_quarters": int(panel["usable"].sum())}
    if not quiet:
        print("=== review panel ===")
        print(f"stores with >=1 review_count: {n_stores}   usable store-quarters: "
              f"{stats['usable_store_quarters']}   Breeze-flagged (excluded): {stats['breeze_excluded']}")
        print(f"% stores with >=12 usable quarters: {pct12:.1f}%")
        print(f"median gap between consecutive snapshots: {med_gap:.0f} days "
              f"(usable pairs only: {med_gap_usable:.0f} days)")
        print("unusable reasons:", panel["unusable_reason"].replace("", "usable").value_counts().to_dict())
        if not yoy.empty:
            with pd.option_context("display.width", 160, "display.max_rows", 200):
                show = yoy.copy()
                show["yoy_agg"] = (100 * show["yoy_agg"]).round(1)
                show["median_store_yoy"] = (100 * show["median_store_yoy"]).round(1)
                for gt, sub in show.groupby("group_type", sort=False):
                    print(f"\n-- same-store YoY new reviews (%), by {gt}: last 8 quarters --")
                    piv = sub.pivot_table(index=["quarter", "fiscal_quarter"], columns="group",
                                          values="yoy_agg").tail(8)
                    n = sub.pivot_table(index="quarter", columns="group", values="n_stores").tail(8)
                    print(piv.to_string())
                    print("n stores:\n" + n.to_string())
        else:
            print("no quarter has same-store matches yet (needs usable data 4 quarters apart)")
        print(f"\n-> {hist / 'review_panel_quarterly.csv'}\n-> {hist / 'review_yoy_summary.csv'}")
    return stats


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--hist", default=str(HIST))
    args = ap.parse_args()
    run(Path(args.hist))
