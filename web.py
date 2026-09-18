"""One HTTP GET with the retry policy every watcher wants."""

from __future__ import annotations

import time
import urllib.error
import urllib.request


def fetch(url: str, user_agent: str, attempts: int = 3) -> str:
    """GET a page or feed, waiting out rate limits.

    Reddit's anonymous RSS allows about one request per 60s window; a 429
    carries x-ratelimit-reset (seconds left in the window), so wait exactly
    that long rather than guessing at a backoff curve. Other sites just get
    the retry-after header or a flat pause.
    """
    last = None
    for i in range(attempts):
        if i:
            time.sleep(_retry_delay(last))
        req = urllib.request.Request(url, headers={
            "User-Agent": user_agent,
            "Accept": "application/atom+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8", "replace")
            if body.strip():
                return body
            last = ("empty", 10)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504):
                raise
            reset = e.headers.get("x-ratelimit-reset") or e.headers.get("retry-after")
            try:
                wait = min(90, int(float(reset)) + 3)
            except (TypeError, ValueError):
                wait = 30
            last = (f"HTTP {e.code}", wait)
        except (urllib.error.URLError, TimeoutError) as e:
            last = (str(e), 15)
    raise RuntimeError(f"fetch failed after {attempts} attempts: {last[0]}")


def _retry_delay(last) -> int:
    return last[1] if last else 15
