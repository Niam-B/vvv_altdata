"""Parsers for www.take5.com sitemap and store pages.

Written without live access to take5.com (blocked from the dev session), from the
formats described in the spec: store URLs /locations/{state}/{city}/{digits}/,
lat/lon in a Google Maps "destination=" link or a static map "center=" param,
"4.6 based on 1,575 Reviews", and "$15 Off ..." offers. JSON-LD is used first when
present. Verify against a real page saved with SAVE_RAW=1 and update
tests/fixtures/take5_store.html if the markup differs.
"""
from __future__ import annotations

import gzip
import re
from typing import Optional
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup

from collectors.parse import _clean, _jsonld_objects, _to_float, _to_int

STORE_PATH_RE = re.compile(r"^/locations/([a-z]{2})/([^/]+)/(\d+)/?$", re.I)
LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)
LATLON = r"(-?\d{1,2}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)"
DEST_RE = re.compile(r"destination=" + LATLON)
CENTER_RE = re.compile(r"center=" + LATLON)
RATING_RE = re.compile(r"([1-5](?:\.\d+)?)\s*(?:stars?\s*)?based\s+on\s+([\d,]+)\s+reviews?", re.I)
PHONE_RE = re.compile(r"\(?\b(\d{3})\)?[\s.-]*(\d{3})[\s.-]*(\d{4})\b")
ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")
OFFER_RE = re.compile(r"(\$\s?\d+(?:\.\d{2})?\s*off\b[^$]{0,160}?)(?=\$\s?\d+\s*off\b|$)", re.I)
DAY_RE = re.compile(r"\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\b", re.I)


# ---------------------------------------------------------------- sitemap
def sitemap_locs(body: bytes) -> tuple[list[str], bool]:
    """(<loc> URLs, is_sitemap_index). Handles gzipped sitemaps."""
    if body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    text = body.decode("utf-8", "replace")
    return [unquote(u).replace("&amp;", "&") for u in LOC_RE.findall(text)], "<sitemapindex" in text


def store_key(url: str) -> Optional[tuple[str, str, str]]:
    """(state, city, store_number) if url is a store page, else None."""
    m = STORE_PATH_RE.match(urlparse(url).path)
    return (m.group(1).upper(), m.group(2), m.group(3)) if m else None


def norm_store_url(url: str) -> str:
    p = urlparse(url)
    return f"https://www.take5.com{p.path.rstrip('/')}/"


# ---------------------------------------------------------------- store page
def _latlon(soup: BeautifulSoup, html: str) -> tuple[Optional[float], Optional[float]]:
    candidates = [a.get("href", "") for a in soup.find_all("a", href=True)]
    candidates += [i.get("src", "") for i in soup.find_all(["img", "iframe"], src=True)]
    for rx in (DEST_RE, CENTER_RE):
        for c in candidates + [html]:
            if m := rx.search(unquote(c)):
                lat, lon = float(m.group(1)), float(m.group(2))
                if -90 <= lat <= 90 and -180 <= lon <= 180:
                    return lat, lon
    return None, None


def _hours(soup: BeautifulSoup) -> str:
    rx = re.compile(r"hours", re.I)
    tag = soup.find(lambda t: rx.search(" ".join(t.get("class", [])) + " " + (t.get("id") or "")))
    if tag:
        return _clean(tag.get_text(" "))
    # fallback: rows/items that start with a day name and contain a time
    rows = [_clean(t.get_text(" ")) for t in soup.find_all(["tr", "li", "p", "div"])
            if not t.find(["tr", "li", "p", "div"])]
    rows = [r for r in rows if DAY_RE.match(r) and re.search(r"\d{1,2}(:\d{2})?\s*[ap]\.?m|closed", r, re.I)]
    return " | ".join(dict.fromkeys(rows))


def _offers(soup: BeautifulSoup) -> str:
    rx = re.compile(r"coupon|offer|promo|deal|special", re.I)

    def is_box(t) -> bool:
        return t.name not in ("script", "style", "meta", "link") and bool(
            rx.search(" ".join(t.get("class", [])) + " " + (t.get("id") or "")))

    boxes = soup.find_all(is_box)
    leaves = [b for b in boxes if not any(is_box(d) for d in b.find_all(True))]
    texts = [_clean(b.get_text(" ")) for b in leaves]
    texts = [t for t in texts if re.search(r"\$\s?\d+|%\s*off|free", t, re.I)]
    if not texts:  # fallback: "$N Off ..." phrases anywhere in the page text
        texts = [_clean(m.group(1)) for m in OFFER_RE.finditer(_clean(soup.get_text(" ")))]
    return " | ".join(dict.fromkeys(texts))


def parse_take5_store(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    st, city, num = store_key(url) or ("", "", "")
    rec = {"store_url": norm_store_url(url), "store_number": num, "state": st, "city": city,
           "street": "", "zip": "", "phone": "", "lat": None, "lon": None,
           "google_rating": None, "review_count": None, "hours_text": "", "coupon_text": ""}

    for o in _jsonld_objects(soup):
        addr = o.get("address")
        if isinstance(addr, dict) and not rec["street"]:
            rec["street"] = _clean(str(addr.get("streetAddress", "")))
            rec["zip"] = _clean(str(addr.get("postalCode", "")))[:5]
            if addr.get("addressLocality"):
                rec["city"] = _clean(str(addr["addressLocality"]))
        if not rec["phone"] and isinstance(o.get("telephone"), str):
            rec["phone"] = o["telephone"]
        geo = o.get("geo")
        if isinstance(geo, dict) and rec["lat"] is None:
            rec["lat"], rec["lon"] = _to_float(geo.get("latitude")), _to_float(geo.get("longitude"))
        agg = o.get("aggregateRating")
        if isinstance(agg, dict) and rec["google_rating"] is None:
            rec["google_rating"] = _to_float(agg.get("ratingValue"))
            rec["review_count"] = _to_int(agg.get("reviewCount") or agg.get("ratingCount"))

    if rec["lat"] is None:
        rec["lat"], rec["lon"] = _latlon(soup, html)

    text = _clean(soup.get_text(" "))
    if rec["google_rating"] is None and (m := RATING_RE.search(text)):
        rec["google_rating"], rec["review_count"] = _to_float(m.group(1)), _to_int(m.group(2))

    if not rec["phone"]:
        tel = soup.find("a", href=re.compile(r"^tel:", re.I))
        rec["phone"] = tel["href"][4:] if tel else (m.group(0) if (m := PHONE_RE.search(text)) else "")
    digits = re.sub(r"\D", "", rec["phone"])
    if len(digits) == 11 and digits[0] == "1":
        digits = digits[1:]
    rec["phone"] = f"({digits[:3]}) {digits[3:6]}-{digits[6:]}" if len(digits) == 10 else rec["phone"]

    if not rec["street"]:
        tag = soup.find("address") or soup.find(attrs={"itemprop": "streetAddress"}) or \
            soup.find(class_=re.compile(r"address", re.I))
        if tag:
            lines = [_clean(x) for x in tag.get_text("\n").split("\n") if _clean(x)]
            if lines:
                rec["street"] = lines[0]
            if m := ZIP_RE.search(" ".join(lines)):
                rec["zip"] = m.group(1)

    rec["hours_text"] = _hours(soup)
    rec["coupon_text"] = _offers(soup)
    return rec


def take5_has_data(html: str) -> bool:
    return bool(RATING_RE.search(html) or DEST_RE.search(unquote(html)) or CENTER_RE.search(unquote(html))
                or "ld+json" in html)
