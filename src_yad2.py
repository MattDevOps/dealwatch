"""yad2.co.il: the main private-seller market, behind Radware Bot Manager.

Plain HTTP, cookie replay, and headless Chrome all get the challenge page;
a real, windowed Chrome driven through patchright (which hides the
DevTools-protocol tells Radware keys on) passes it. So this source needs
`pip install patchright`, google-chrome, and a display (Xvfb is fine).
The optional import is deferred so the rest of carwatch, and the tests,
run without any of that.
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path

from listings import Listing, hp_from_text
from nextjs import page_props as next_props

BASE = "https://www.yad2.co.il"

DEFAULTS = {
    # Hyundai = 21, Ioniq 5 = 11239 (yad2's own ids, from its filter URLs).
    "yad2_manufacturer": 21,
    "yad2_model": 11239,
    "yad2_year_from": 2024,
    "yad2_year_to": 2026,
    "yad2_challenge_seconds": 40,
}
FEED_KEYS = ("feed", "feed-mix")
# Trim is the sub-model text up to the drivetrain words:
# "4X2 Luxury חשמלי אוט׳ (217 כ״ס)" -> "4X2 Luxury"
TRIM_RE = re.compile(r"^(.*?)\s*(?:חשמלי|היברידי|בנזין|דיזל|אוט|\()")


def search_url(cfg: dict) -> str:
    return (f"{BASE}/vehicles/cars?manufacturer={int(cfg['yad2_manufacturer'])}"
            f"&model={int(cfg['yad2_model'])}"
            f"&year={int(cfg['yad2_year_from'])}-{int(cfg['yad2_year_to'])}")


def item_url(token: str) -> str:
    return f"{BASE}/vehicles/item/{token}"


def _queries(page: dict) -> list[dict]:
    return page.get("dehydratedState", {}).get("queries", [])


def _text(node) -> str:
    return node.get("text", "") if isinstance(node, dict) else ""


def _trim(sub_model: str) -> str:
    m = TRIM_RE.match(sub_model)
    return (m.group(1) if m else sub_model).strip()


def _base_listing(item: dict) -> Listing:
    sub = _text(item.get("subModel"))
    addr = item.get("address") or {}
    seller = item.get("adType") or ""
    agency = (item.get("customer") or {}).get("agencyName")
    if agency:
        seller = agency.strip()
    dates = item.get("vehicleDates") or {}
    return Listing(
        source="yad2", id=item["token"], url=item_url(item["token"]),
        year=dates.get("yearOfProduction"),
        trim=_trim(sub), hp=hp_from_text(sub),
        price=item.get("price") or None,
        hand=_text(item.get("hand")),
        city=_text(addr.get("city")) or _text(addr.get("area")),
        seller=seller,
        created_at=(item.get("createdAt") or "")[:10],
        tags=["auction"] if item.get("externalKonesUrl") else [],
    )


def parse_feed(page: dict) -> list[Listing]:
    """Every ad in the search payload, whichever feed layout yad2 is A/B
    testing: the classic one splits ads into commercial/private/platinum
    lists, the mixed one puts them all under `ads`."""
    seen, out = set(), []
    for q in _queries(page):
        key = q.get("queryKey") or []
        if not key or key[0] not in FEED_KEYS:
            continue
        data = q.get("state", {}).get("data") or {}
        for value in data.values():
            if not isinstance(value, list):
                continue
            for item in value:
                token = item.get("token") if isinstance(item, dict) else None
                if token and token not in seen:      # slotTypes etc. have none
                    seen.add(token)
                    out.append(_base_listing(item))
    return out


def parse_item(lst: Listing, page: dict, now: datetime | None = None) -> Listing | None:
    """Fill from the ad's own page; None once the ad is gone or expired."""
    item = None
    for q in _queries(page):
        key = q.get("queryKey") or []
        if len(key) >= 3 and key[0] == "vehicles" and key[1] == "item":
            item = q.get("state", {}).get("data")
    if not item or not item.get("token"):
        return None
    ends = (item.get("dates") or {}).get("endsAt")
    if ends and ends[:10] < (now or datetime.now()).strftime("%Y-%m-%d"):
        return None
    fresh = _base_listing(item)
    fresh.hp = item.get("horsePower") or fresh.hp
    fresh.km = item.get("km")
    fresh.range_km = item.get("allElectricRange") or ""
    fresh.description = (item.get("metaData") or {}).get("description") or ""
    month = _text((item.get("vehicleDates") or {}).get("monthOfProduction"))
    if month:
        fresh.tags.append(month)
    return lst.updated_with(fresh)


class Browser:
    """One Chrome per poll, shared by the search and every item check."""

    def __init__(self, profile_dir, challenge_seconds: int = 40):
        self.profile_dir = str(profile_dir)
        self.challenge_seconds = challenge_seconds
        self._pw = self._ctx = self._page = None

    def __enter__(self):
        from patchright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._ctx = self._pw.chromium.launch_persistent_context(
            user_data_dir=self.profile_dir, channel="chrome", headless=False,
            no_viewport=True, locale="he-IL",
        )
        self._page = self._ctx.new_page()
        return self

    def __exit__(self, *exc):
        for closer in (self._ctx.close, self._pw.stop):
            try:
                closer()
            except Exception:
                pass

    def page_props(self, url: str) -> dict | None:
        """Load a yad2 page and return its Next.js props, waiting out the
        bot-manager interstitial. A captcha that never clears is an error,
        not an empty result: nothing must be marked seen on its account."""
        self._page.goto(url, wait_until="domcontentloaded")
        deadline = time.time() + self.challenge_seconds
        while True:
            props = next_props(self._page.content())
            if props is not None and "perfdrive" not in self._page.url:
                return props
            if time.time() > deadline:
                raise RuntimeError(
                    f"yad2 bot wall did not clear for {url} "
                    f"(title {self._page.title()!r})")
            time.sleep(2)


class Source:
    name = "yad2"

    def __init__(self, cfg: dict, state_dir):
        self.cfg = cfg
        self.browser = Browser(Path(state_dir) / "chrome-profile",
                               int(cfg.get("yad2_challenge_seconds", 40)))

    def __enter__(self):
        self.browser.__enter__()
        return self

    def __exit__(self, *exc):
        return self.browser.__exit__(*exc)

    def search(self) -> list[Listing]:
        return parse_feed(self.browser.page_props(search_url(self.cfg)) or {})

    def verify(self, lst: Listing) -> Listing | None:
        return parse_item(lst, self.browser.page_props(lst.url) or {})
