"""HTML parsers for store.vioc.com and jobs.vioc.com.

These were written without live access to the site, so they deliberately avoid
depending on specific CSS class names. They key off URL structure
(/{st}, /{st}/{city}, /{st}/{city}/{slug}), JSON-LD / meta tags, and text
patterns ("0-10 Minutes", "Showing 1-20 of N jobs"), with class-name hints only
as a secondary signal. If a field comes back empty on the real site, inspect a
page saved with SAVE_RAW=1 and tighten the relevant function here.
"""
from __future__ import annotations

import json
import re
from typing import Iterable, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

STORE_HOST = "store.vioc.com"

WAIT_RE = re.compile(r"(\d+\s*[-–]\s*\d+|\d+\s*\+)\s*Min(?:ute)?s?", re.I)
RATING_RE = re.compile(r"\b([1-5](?:\.\d)?)\s*(?:out of 5|stars?|★)", re.I)
REVIEWS_RE = re.compile(r"([\d,]+)\s+(?:Google\s+)?reviews?\b", re.I)
PLACE_ID_RES = [
    re.compile(r"(?:query_place_id|place_id|placeid)[=:]([A-Za-z0-9_-]{16,})", re.I),
    re.compile(r"\b(ChIJ[A-Za-z0-9_-]{16,})"),
]
AMOUNT_RE = re.compile(r"(\$\s?\d+(?:\.\d{2})?\s*off|\d{1,2}\s?%\s*off|\$\s?\d+(?:\.\d{2})?)", re.I)
EXPIRY_RE = re.compile(
    r"(?:exp(?:ires|iration)?\.?|valid\s+(?:through|thru|until)|offer\s+ends)\s*:?\s*"
    r"(\d{1,2}/\d{1,2}/\d{2,4}|[A-Z][a-z]{2,8}\.?\s+\d{1,2},?\s+\d{4}|\d{4}-\d{2}-\d{2})",
    re.I,
)
JOBS_TOTAL_RE = re.compile(r"Showing\s+[\d,]+\s*[-–]\s*[\d,]+\s+of\s+([\d,]+)\s+jobs?", re.I)
LABEL_RE = re.compile(r"\bVIOC[A-Z]*\b")
CITY_ST_RE = re.compile(r"\b([A-Z][A-Za-z .'-]+,\s*[A-Z]{2}(?:\s+\d{5})?)\b")
STREET_RE = re.compile(
    r"\b\d{1,6}\s+[A-Za-z0-9 .'-]+?\s(?:St|Street|Ave|Avenue|Rd|Road|Blvd|Boulevard|Dr|Drive|Hwy|Highway|"
    r"Pkwy|Parkway|Ln|Lane|Way|Pike|Ct|Court|Pl|Place|Cir|Circle|Trl|Trail|Tpke|Sq|Loop|Rte|Route)\b\.?"
    r"[^\n]{0,60}?\b[A-Z]{2}\s+\d{5}",
)


def soup_of(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def _clean(s: Optional[str]) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def norm_url(href: str, base: str) -> Optional[str]:
    """Absolute, fragment/query-free, no trailing slash or .html; None if off-site."""
    if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
        return None
    u = urlparse(urljoin(base, href))
    if u.netloc and u.netloc.lower() != STORE_HOST:
        return None
    path = re.sub(r"(/index)?\.html?$", "", u.path).rstrip("/")
    return f"https://{STORE_HOST}{path}"


def path_parts(url: str) -> list[str]:
    return [p for p in urlparse(url).path.split("/") if p]


def _links(soup: BeautifulSoup, base: str) -> Iterable[tuple[Tag, str]]:
    for a in soup.find_all("a", href=True):
        u = norm_url(a["href"], base)
        if u:
            yield a, u


# ---------------------------------------------------------------- index / state
def parse_index(html: str, base: str) -> list[str]:
    """State URLs (/{st})."""
    out = {u for _, u in _links(soup_of(html), base)
           if len(p := path_parts(u)) == 1 and re.fullmatch(r"[a-z]{2}", p[0], re.I)}
    return sorted(out)


def parse_state(html: str, base: str, st: str) -> tuple[list[str], list[str]]:
    """(city URLs, direct store URLs) linked from a state page."""
    cities, stores = set(), set()
    for _, u in _links(soup_of(html), base):
        p = path_parts(u)
        if p and p[0].lower() == st.lower():
            if len(p) == 2:
                cities.add(u)
            elif len(p) == 3:
                stores.add(u)
    return sorted(cities), sorted(stores)


# ---------------------------------------------------------------- city page
def _container_for(anchor: Tag, url: str, store_urls_in: dict) -> Tag:
    """Largest ancestor of ``anchor`` containing links to this store only."""
    node = anchor
    while node.parent is not None and node.parent.name not in ("body", "html", "[document]"):
        parent = node.parent
        if store_urls_in(parent) - {url}:
            break
        node = parent
    return node


def _class_hint(tag: Tag, pattern: str) -> Optional[Tag]:
    rx = re.compile(pattern, re.I)
    return tag.find(lambda t: isinstance(t, Tag) and (
        rx.search(" ".join(t.get("class", []))) or rx.search(t.get("id", "") or "")
        or rx.search(t.get("itemprop", "") or "")))


def norm_wait(text: str) -> str:
    m = WAIT_RE.search(text or "")
    if not m:
        return ""
    return re.sub(r"\s+", "", m.group(1)).replace("–", "-")


def parse_city(html: str, base: str) -> list[dict]:
    """Stores listed on a city page: store_url, name, address, wait_bucket, hours_text, services."""
    soup = soup_of(html)
    anchors: dict[str, Tag] = {}
    for a, u in _links(soup, base):
        if len(path_parts(u)) == 3:
            anchors.setdefault(u, a)
    if not anchors:
        return []

    def store_urls_in(tag: Tag) -> set:
        return {u for a, u in _links(tag, base) if len(path_parts(u)) == 3}

    out = []
    for url, a in anchors.items():
        box = _container_for(a, url, store_urls_in)
        text = _clean(box.get_text(" "))
        heading = box.find(re.compile(r"^h[1-5]$"))
        name = _clean(heading.get_text(" ")) if heading else ""
        if not name:
            texts = [_clean(x.get_text(" ")) for x, u in _links(box, base) if u == url]
            texts = [t for t in texts if t and not re.search(r"details|visit|view|more|coupon", t, re.I)]
            name = texts[0] if texts else ""
        addr_tag = box.find("address") or _class_hint(box, r"address|streetAddress")
        address = _clean(addr_tag.get_text(" ")) if addr_tag else ""
        if not address and (m := STREET_RE.search(text)):
            address = _clean(m.group(0))
        hours_tag = _class_hint(box, r"hours|openingHours")
        hours = _clean(hours_tag.get_text(" ")) if hours_tag else ""
        if not hours and (m := re.search(r"((?:Open|Closed)[^.|]{0,60})", text)):
            hours = _clean(m.group(1))
        svc_tag = _class_hint(box, r"service")
        services = ""
        if svc_tag:
            items = [_clean(li.get_text(" ")) for li in svc_tag.find_all("li")]
            services = "|".join(i for i in items if i) or _clean(svc_tag.get_text(" "))
        out.append({
            "store_url": url,
            "name": name,
            "address": address,
            "wait_bucket": norm_wait(text),
            "hours_text": hours,
            "services": services,
        })
    return out


def city_has_data(html: str) -> bool:
    return bool(WAIT_RE.search(html)) or bool(
        re.search(r'href="[^"]*/[a-z]{2}/[^/"]+/[^/"]+"', html, re.I))


# ---------------------------------------------------------------- store page
def _jsonld_objects(soup: BeautifulSoup) -> list[dict]:
    objs: list[dict] = []

    def walk(x):
        if isinstance(x, dict):
            objs.append(x)
            for v in x.values():
                if isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    for s in soup.find_all("script", type=re.compile("ld\\+json", re.I)):
        try:
            walk(json.loads(s.string or s.get_text() or ""))
        except (ValueError, TypeError):
            continue
    return objs


def _meta(soup: BeautifulSoup, *names: str) -> str:
    for n in names:
        t = (soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
             or soup.find(attrs={"itemprop": n}))
        if t is not None:
            v = t.get("content") or t.get_text()
            if _clean(v):
                return _clean(v)
    return ""


def _to_float(s) -> Optional[float]:
    try:
        return float(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _to_int(s) -> Optional[int]:
    f = _to_float(s)
    return int(f) if f is not None else None


def parse_coupons(soup: BeautifulSoup) -> list[dict]:
    rx = re.compile(r"coupon|offer|promo|special|deal", re.I)

    def is_box(t) -> bool:
        if not isinstance(t, Tag) or t.name in ("script", "style", "a", "meta", "link"):
            return False
        return bool(rx.search(" ".join(t.get("class", [])) + " " + (t.get("id") or "")))

    boxes = soup.find_all(is_box)
    # keep innermost boxes that look like an individual coupon
    leaves = [b for b in boxes if not any(is_box(d) for d in b.find_all(True))]
    seen, out = set(), []
    for b in leaves:
        text = _clean(b.get_text(" "))
        if len(text) < 6 or not (AMOUNT_RE.search(text) or "off" in text.lower()):
            continue
        if text in seen:
            continue
        seen.add(text)
        amt = AMOUNT_RE.search(text)
        exp = EXPIRY_RE.search(text)
        out.append({
            "amount": _clean(amt.group(1)) if amt else "",
            "offer": text[:200],
            "expiry": exp.group(1) if exp else "",
            "new_customers_only": bool(re.search(r"new\s+customers?\s+only", text, re.I)),
        })
    return out


def parse_store(html: str, url: str) -> dict:
    """Store page -> name, address, lat, lon, place_id, rating, review_count, coupons."""
    soup = soup_of(html)
    objs = _jsonld_objects(soup)
    rec: dict = {"store_url": url, "name": "", "address": "", "lat": None, "lon": None,
                 "place_id": "", "rating": None, "review_count": None}

    for o in objs:
        if not rec["name"] and o.get("@type") not in ("BreadcrumbList", "ListItem", "WebPage", "Review") \
                and isinstance(o.get("name"), str) and ("address" in o or "geo" in o):
            rec["name"] = _clean(o["name"])
        addr = o.get("address")
        if not rec["address"] and isinstance(addr, dict):
            rec["address"] = _clean(" ".join(str(addr.get(k, "")) for k in (
                "streetAddress", "addressLocality", "addressRegion", "postalCode")))
        geo = o.get("geo")
        if rec["lat"] is None and isinstance(geo, dict):
            rec["lat"], rec["lon"] = _to_float(geo.get("latitude")), _to_float(geo.get("longitude"))
        agg = o.get("aggregateRating")
        if rec["rating"] is None and isinstance(agg, dict):
            rec["rating"] = _to_float(agg.get("ratingValue"))
            rec["review_count"] = _to_int(agg.get("reviewCount") or agg.get("ratingCount"))

    if rec["lat"] is None:
        rec["lat"] = _to_float(_meta(soup, "place:location:latitude", "latitude", "geo.latitude"))
        rec["lon"] = _to_float(_meta(soup, "place:location:longitude", "longitude", "geo.longitude"))
    if rec["lat"] is None and (gp := _meta(soup, "geo.position", "ICBM")):
        parts = re.split(r"[;,]", gp)
        if len(parts) == 2:
            rec["lat"], rec["lon"] = _to_float(parts[0]), _to_float(parts[1])

    # Place ID: prefer the review link hrefs, then anywhere in the HTML
    hrefs = " ".join(a["href"] for a in soup.find_all("a", href=True))
    for hay in (hrefs, html):
        for rx in PLACE_ID_RES:
            if m := rx.search(hay):
                rec["place_id"] = m.group(1)
                break
        if rec["place_id"]:
            break

    text = _clean(soup.get_text(" "))
    if rec["rating"] is None and (m := RATING_RE.search(text)):
        rec["rating"] = _to_float(m.group(1))
    if rec["review_count"] is None and (m := REVIEWS_RE.search(text)):
        rec["review_count"] = _to_int(m.group(1))
    if not rec["name"] and (h1 := soup.find("h1")):
        rec["name"] = _clean(h1.get_text(" "))
    if not rec["address"]:
        tag = soup.find("address") or soup.find(attrs={"itemprop": "streetAddress"})
        if tag:
            rec["address"] = _clean(tag.get_text(" "))
        elif m := STREET_RE.search(text):
            rec["address"] = _clean(m.group(0))

    rec["coupons"] = parse_coupons(soup)
    return rec


def store_has_data(html: str) -> bool:
    return "ld+json" in html or bool(re.search(r"coupon|reviews?\b|latitude", html, re.I))


# ---------------------------------------------------------------- jobs
def parse_jobs_total(html: str) -> Optional[int]:
    m = JOBS_TOTAL_RE.search(_clean(soup_of(html).get_text(" ")))
    return int(m.group(1).replace(",", "")) if m else None


def parse_jobs(html: str, base: str) -> list[dict]:
    """Job listings on one results page: job_url, title, address, label."""
    soup = soup_of(html)
    anchors: dict[str, Tag] = {}
    for a in soup.find_all("a", href=True):
        u = urljoin(base, a["href"]).split("#")[0]
        p = urlparse(u).path
        if re.search(r"/jobs?/[^/]+", p) and "/page/" not in p and not p.rstrip("/").endswith("service-center-jobs"):
            anchors.setdefault(u, a)

    def job_urls_in(tag: Tag) -> set:
        return {urljoin(base, x["href"]).split("#")[0] for x in tag.find_all("a", href=True)} & anchors.keys()

    out = []
    for url, a in anchors.items():
        box = _container_for(a, url, job_urls_in)
        text = _clean(box.get_text(" "))
        title = _clean(a.get_text(" "))
        if not title and (h := box.find(re.compile(r"^h[1-5]$"))):
            title = _clean(h.get_text(" "))
        loc_tag = _class_hint(box, r"location|address|city")
        address = _clean(loc_tag.get_text(" ")) if loc_tag else ""
        if not address and (m := STREET_RE.search(text) or CITY_ST_RE.search(text)):
            address = _clean(m.group(0))
        labels = set(LABEL_RE.findall(text))
        tag_box = _class_hint(box, r"tag|label|badge|brand|category")
        if tag_box:
            labels.update(t for t in re.split(r"\s*[|,•]\s*", _clean(tag_box.get_text(" "))) if t)
        out.append({"job_url": url, "title": title, "address": address,
                    "label": "|".join(sorted(labels))})
    return out


def jobs_has_data(html: str) -> bool:
    return parse_jobs_total(html) is not None
