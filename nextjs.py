"""Both car sites are Next.js apps that ship the page's data inline."""

from __future__ import annotations

import json
import re

NEXT_DATA = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)


def page_props(html: str) -> dict | None:
    """The pageProps of a server-rendered Next.js page, or None when the
    page is not one (a bot-wall interstitial, an error page)."""
    m = NEXT_DATA.search(html)
    return json.loads(m.group(1))["props"]["pageProps"] if m else None
