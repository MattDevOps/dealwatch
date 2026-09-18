"""carwiz.co.il: dealer inventory, served as plain HTML with a Next.js
payload. No bot wall, so stdlib fetching is enough.

Carwiz files every Ioniq (hybrid, Electric, 5, 6) under one model,
"איוניק", so the search page is only a candidate list: the detail page's
body type (MPV for the Ioniq 5, sedan for the 6), fuel type, battery and
power decide.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.parse

from listings import Listing
from nextjs import page_props
from web import fetch

BASE = "https://carwiz.co.il"
SEARCH_PATH = "/used-cars/{make}/{model}/year-{year_from}-{year_to}"
NUM = re.compile(r"\d[\d,]*")

DEFAULTS = {
    "carwiz_make": "יונדאי",
    "carwiz_model": "איוניק",
    "carwiz_year_from": 2024,
    "carwiz_year_to": 2026,
}
IONIQ5_BODIES = ("mpv", "crossover", "קרוסאובר", "פנאי")


def next_data(html: str) -> dict:
    props = page_props(html)
    if props is None:
        raise ValueError("no __NEXT_DATA__ in page")
    return props


def search_url(cfg: dict) -> str:
    return BASE + SEARCH_PATH.format(
        make=urllib.parse.quote(cfg["carwiz_make"]),
        model=urllib.parse.quote(cfg["carwiz_model"]),
        year_from=int(cfg["carwiz_year_from"]),
        year_to=int(cfg["carwiz_year_to"]))


def _first_int(text: str) -> int | None:
    m = NUM.search(text or "")
    return int(m.group().replace(",", "")) if m else None


def parse_search(page: dict) -> list[Listing]:
    """Cards from the results page. Promo slots share the list (no carId,
    or isPromotion with a fake one), so they are skipped."""
    out = []
    for p in page.get("resultsPage", {}).get("posts", []):
        if not p.get("carId") or p.get("isPromotion") or not p.get("link"):
            continue
        specs = p.get("specs") or []
        out.append(Listing(
            source="carwiz", id=p["carId"], url=BASE + p.get("link", ""),
            year=_first_int(specs[0]) if specs else None,
            trim=p.get("finishLevel") or "",
            price=p.get("price"),
            km=_first_int(specs[2]) if len(specs) > 2 else None,
            hand=specs[1] if len(specs) > 1 else "",
            range_km=str(_first_int(specs[3]) or "") if len(specs) > 3 else "",
            city=p.get("city") or "",
            seller="dealer",
        ))
    return out


def parse_detail(lst: Listing, page: dict) -> Listing | None:
    """Fill the listing from its detail page; None if it is gone.

    A dead id does not 404: carwiz serves the search page instead, which
    has no extendedPost. An unpublished or deleted post is gone too.
    """
    post = page.get("extendedPost")
    if not post or not post.get("publish") or post.get("deletedAt"):
        return None
    spec = post.get("specification") or {}
    body = (spec.get("body") or "").lower()
    fuel = spec.get("fuelType") or ""
    if fuel and fuel != "חשמל":
        lst.model = f"ioniq hybrid ({fuel})"
    elif body and not any(b in body for b in IONIQ5_BODIES):
        lst.model = f"ioniq 6? (body {spec.get('body')})"
    lst.year = spec.get("year") or post.get("year") or lst.year
    lst.month = post.get("monthOnRoad")
    lst.trim = spec.get("finishLevel") or lst.trim
    lst.hp = spec.get("maximumHorsePower") or lst.hp
    lst.battery_kwh = spec.get("batteryCapacityKwh")
    lst.range_km = str(spec.get("drivingRange") or lst.range_km or "")
    lst.price = post.get("priceV2") or post.get("price") or lst.price
    lst.km = post.get("kilometrage") or lst.km
    lst.created_at = (post.get("createdAt") or "")[:10]
    lst.description = post.get("remark") or ""
    branch = post.get("agencyBranch") or {}
    agency = (branch.get("agency") or {}).get("displayName")
    if agency:
        lst.seller = agency
    if branch.get("city"):
        lst.city = branch["city"]
    return lst


class Source:
    """The per-poll handle carwatch drives: open, search, verify each
    candidate, close. Nothing to hold open here; yad2's holds a browser."""
    name = "carwiz"

    def __init__(self, cfg: dict, state_dir=None):
        self.cfg = cfg

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def search(self) -> list[Listing]:
        html = fetch(search_url(self.cfg), self.cfg["user_agent"])
        return parse_search(next_data(html))

    def verify(self, lst: Listing) -> Listing | None:
        try:
            html = fetch(lst.url, self.cfg["user_agent"])
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                return None
            raise
        return parse_detail(lst, next_data(html))
