#!/usr/bin/env python3
"""dealwatch - alert on new r/buildapcsales posts matching a wishlist.

Default target: RTX 5090 GPUs and 5090 prebuilt desktops, with bonus scoring
for 9950X3D / 9900X3D CPUs and 64GB RAM.

Pure stdlib. Sources the subreddit's Atom feed (reddit's .json endpoint is
now behind a browser interstitial; .rss is not).
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

ATOM = {"a": "http://www.w3.org/2005/Atom"}
HOME = Path(__file__).resolve().parent
DEFAULT_STATE = HOME / "state" / "seen.json"
DEFAULT_LOG = HOME / "state" / "hits.jsonl"

DEFAULT_CONFIG = {
    "subreddit": "buildapcsales",
    "limit": 50,
    "user_agent": "dealwatch/1.0 (personal deal alerter)",
    # A post must match one of these to alert on.
    "must_match": [r"\b(rtx\s*)?5090\b"],
    # Any hit kills the alert (laptop 5090s are a different, much slower part).
    "exclude": [r"\blaptop\b", r"\bmobile\b", r"\[laptop\]", r"\bhandheld\b"],
    # Flairs that can never hold the deal we want, even if "5090" is in the
    # title (e.g. a monitor bundled with a 5090 rig). Everything else passes.
    "deny_flairs": ["monitor", "case", "psu", "ssd", "storage", "keyboard",
                    "mouse", "cooler", "fan", "headphones", "speakers", "tv",
                    "cable", "dock", "laptop", "chair", "software"],
    # Extra credit; each adds to the score and is shown as a tag in the alert.
    "bonus": [
        {"label": "9950X3D", "pattern": r"\b9950\s*x3d\b", "points": 3},
        {"label": "9900X3D", "pattern": r"\b9900\s*x3d\b", "points": 3},
        {"label": "9800X3D", "pattern": r"\b9800\s*x3d\b", "points": 2},
        {"label": "7950X3D", "pattern": r"\b7950\s*x3d\b", "points": 1},
        {"label": "64GB", "pattern": r"\b64\s*gb\b", "points": 2},
        {"label": "FE", "pattern": r"\bfounders\b", "points": 1},
    ],
    # Price at or under this is flagged as a hot deal (in the alert title).
    "target_price_gpu": 2200,
    "target_price_desktop": 3800,
    # False = alert on every 5090 regardless of price (missing a deal costs
    # more than an extra ping). True = only alert under the target price.
    "hard_price_filter": False,
    "ntfy_topic": "",
    "ntfy_server": "https://ntfy.sh",
    # Telegram is used when a bot token and chat id turn up in the
    # environment or in one of these files (daytrader already writes one).
    "telegram": True,
    "telegram_env_files": ["~/.config/dealwatch.env", "~/.config/daytrader.env"],
    "desktop_notify": True,
    "auto_open_hot": False,
}


# ---------------------------------------------------------------- config/state

def load_config(path: Path) -> dict:
    """DEFAULT_CONFIG, then config.json, then config.local.json.

    The .local file is gitignored and is where anything private belongs --
    the ntfy topic is a shared secret, so it must never reach a public repo.
    """
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    for layer in (path, path.with_name(path.stem + ".local" + path.suffix)):
        if layer.exists():
            for k, v in json.loads(layer.read_text()).items():
                cfg[k] = v
    return cfg


def validate_config(cfg: dict) -> None:
    """Compile every user-supplied pattern now.

    A typo in config.json must fail loudly at startup, not silently skip
    posts one at a time for a week.
    """
    for key in ("must_match", "exclude"):
        for pattern in cfg.get(key, []):
            try:
                re.compile(pattern)
            except re.error as e:
                raise ValueError(f"bad regex in {key}: {pattern!r} ({e})") from e
    for rule in cfg.get("bonus", []):
        if "label" not in rule or "pattern" not in rule:
            raise ValueError(f"bonus rule needs label and pattern: {rule!r}")
        try:
            re.compile(rule["pattern"])
        except re.error as e:
            raise ValueError(
                f"bad regex in bonus {rule['label']!r}: {rule['pattern']!r} ({e})") from e


def load_seen(path: Path) -> list[str]:
    """Post ids, oldest first. A list, not a set: the trim in save_seen
    drops the oldest, and set iteration order would make that arbitrary."""
    if not path.exists():
        return []
    try:
        return list(json.loads(path.read_text()).get("seen", []))
    except (json.JSONDecodeError, OSError):
        return []


def save_seen(path: Path, seen: list[str], keep: int = 3000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"seen": seen[-keep:]}))
    tmp.replace(path)


# ------------------------------------------------------------------- fetching

def fetch(url: str, user_agent: str, attempts: int = 3) -> str:
    """GET the feed, honouring reddit's rate limit.

    Anonymous RSS allows about one request per 60s window; a 429 carries
    x-ratelimit-reset (seconds left in the window), so wait exactly that
    long rather than guessing at a backoff curve.
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


def feed_url(cfg: dict) -> str:
    return (f"https://www.reddit.com/r/{cfg['subreddit']}/new/.rss"
            f"?limit={int(cfg['limit'])}")


# -------------------------------------------------------------------- parsing

@dataclass
class Post:
    id: str
    title: str
    link: str
    author: str = ""
    published: str = ""

    @property
    def flair(self) -> str:
        """Normalised leading [flair], e.g. "[Pre-Built]" -> "prebuilt"."""
        m = re.match(r"\s*\[([^\]]+)\]", self.title)
        if not m:
            return ""
        return re.sub(r"[^a-z0-9 ]", "", m.group(1).strip().lower())


def parse_feed(xml_text: str) -> list[Post]:
    root = ET.fromstring(xml_text)
    posts = []
    for e in root.findall("a:entry", ATOM):
        def text(tag: str) -> str:
            node = e.find(f"a:{tag}", ATOM)
            return (node.text or "").strip() if node is not None else ""

        link_node = e.find("a:link", ATOM)
        link = link_node.attrib.get("href", "") if link_node is not None else ""
        author = ""
        author_node = e.find("a:author/a:name", ATOM)
        if author_node is not None:
            author = (author_node.text or "").strip()
        pid = text("id") or link
        posts.append(Post(
            id=pid,
            title=html.unescape(text("title")),
            link=link,
            author=author,
            published=text("updated") or text("published"),
        ))
    return posts


PRICE_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{2}))?")

# Words marking an amount as the *old* price: "was $2,199", "MSRP $1999",
# "$2,499 retail". Not what you pay, but the same order of magnitude.
WAS_WORDS = (r"was|were|orig(?:inal(?:ly)?)?|reg(?:ular(?:ly)?)?|normally|"
             r"retail|list(?:ed)?|msrp|down\s+from|usually|typically|"
             r"total\s+(value|retail|msrp)|value|worth")
# Words marking an amount as a *discount*: "$300 off", "rebate $200".
# Much smaller than the price and never a substitute for it.
CUT_WORDS = (r"off|back|rebate|credit|gift|coupon|discount|savings?|promo|"
             r"instant|save|clip|slickdeal")

WAS_BEFORE = re.compile(r"\b(%s)\W*$" % WAS_WORDS, re.I)
WAS_AFTER = re.compile(r"^\W*(%s)\b" % WAS_WORDS, re.I)
CUT_BEFORE = re.compile(r"\b(%s)\W*$" % CUT_WORDS, re.I)
CUT_AFTER = re.compile(r"^\W*(%s)\b" % CUT_WORDS, re.I)
# A comma/paren/dash ends a clause: in "$50 off, now $1899" the word "off"
# describes the $50 behind it, not the $1899 ahead of it.
CLAUSE_BREAK = re.compile(r"[,;:()\[\]|/]|\s[-\u2013\u2014]\s")
# "$2897 total", "$3,199 all in", "$2,999 out the door"
TOTAL_RE = re.compile(
    r"\$\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{2}))?\s*"
    r"(?:total(?!\s+(?:value|retail|msrp|savings?))|all[- ]in|"
    r"out\s+the\s+door|otd)\b", re.I)


def parse_price(title: str) -> float | None:
    """The amount you actually pay.

    Titles mix the asking price with was-prices and rebate amounts
    ("RTX 5090 - $1,949 (was $2,199)", "$1899 after $200 rebate"), so
    discard amounts whose surrounding words mark them as either, then take
    the first survivor.
    """
    # "RTX 5090 ($1999) + 9950X3D ($699) = $2897 total" -- when a title adds
    # its parts up, the total is what you pay.
    total = TOTAL_RE.search(title)
    if total:
        whole, cents = total.group(1), total.group(2)
        return float(whole.replace(",", "")) + (float(cents) / 100 if cents else 0)

    clean, was_priced = [], []
    prev_end = 0
    for m in PRICE_RE.finditer(title):
        whole, cents = m.group(1), m.group(2)
        val = float(whole.replace(",", "")) + (float(cents) / 100 if cents else 0)
        # Only the words since the previous amount can qualify this one, and
        # only those in the same clause as it.
        head = CLAUSE_BREAK.split(title[prev_end:m.start()])[-1]
        prev_end = m.end()
        if val < 100:
            continue
        # Look only as far as the next amount and only within this clause, so
        # "$1899 after $200 rebate" does not tar the $1899 with the rebate's
        # wording, nor "$1,949 (was $2,199)" the $1,949 with the was-price's.
        tail = CLAUSE_BREAK.split(title[m.end():].split("$", 1)[0])[0][:14]
        if CUT_BEFORE.search(head) or CUT_AFTER.search(tail):
            continue                      # a discount, never the price
        if WAS_BEFORE.search(head) or WAS_AFTER.search(tail):
            was_priced.append(val)        # the old price: last resort only
            continue
        clean.append(val)
    if clean:
        return clean[0]
    # Every amount was qualified. A was-price is at least the right ballpark
    # (and the largest of them is the item, not an accessory); a discount
    # amount never is, so it is not a candidate here.
    return max(was_priced) if was_priced else None


# -------------------------------------------------------------------- matching

# Words that mean a whole machine. "build" and "combo" only count when
# something makes them a noun about the machine -- otherwise they fire on
# "excellent build quality" and "XLR8 Combo Pack (card + backplate)".
RIG_WORDS = (r"\b(gaming\s*(pc|rig|system|computer|setup)|desktop|tower|prebuilt|"
             r"pre-built|workstation|rig\b|"
             r"(custom|full|complete|gaming|new|entire)\s+(build|combo|system|setup|kit|package)|"
             r"build\s+(w/|with|incl|featuring)\b)")

# Parts other than the card itself. A title listing any of them alongside the
# 5090 is describing a machine, whatever words it uses to say so -- more
# reliable than trying to enumerate every way people write "bundle".
COMPONENTS = {
    "cpu": r"\b(ryzen|threadripper|core\s*(i[3579]|ultra)|i[3579]-\d{3,5}|"
           r"\d{4}\s*x3d|\d{4}x\b|\b(9950|9900|9800|7950|7800)\b|\bcpu\b)",
    "mobo": r"\b(motherboard|mobo|[bxz]\d{3}[a-z]?\b)",
    # \bddr does not match inside "gddr7", so card VRAM does not count.
    "ram": r"\b(ddr[45]|\d{1,3}\s*gb\s*(ram|ddr|memory)|ram\b)",
    "storage": r"\b(nvme|ssd|\d+\s*tb\b)",
    "psu": r"\b(psu|power\s+supply)\b",
    # not bare "case": "in case anyone is interested" is not a PC case
    "case": r"\b(chassis|pc\s+case|mid[- ]tower|full[- ]tower|case\s+included)\b",
    "cooler": r"\b(aio|air\s+cooler|liquid\s+cool)",
}


def components(title: str) -> set[str]:
    """Which non-GPU parts the title mentions."""
    return {name for name, pat in COMPONENTS.items()
            if re.search(pat, title, re.I)}


@dataclass
class Match:
    post: Post
    price: float | None = None
    score: int = 0
    hot: bool = False
    reasons: list[str] = field(default_factory=list)

    @property
    def kind(self) -> str:
        """A whole rig or a bare card. Decides which price cap applies.

        Flair alone is not enough: full builds show up under [Bundle] and
        [CPU] too, so a title carrying both a CPU and the card counts as a
        desktop and gets the (higher) desktop cap.
        """
        f = self.post.flair
        if any(x in f for x in ("desktop", "prebuilt", "built", "pc")):
            return "desktop"
        t = self.post.title
        if re.search(RIG_WORDS, t, re.I) or components(t):
            return "desktop"
        return "gpu"


def _any(patterns, text: str) -> bool:
    return any(re.search(p, text, re.I) for p in patterns)


def evaluate(post: Post, cfg: dict) -> Match | None:
    title = post.title
    if not _any(cfg["must_match"], title):
        return None
    if _any(cfg["exclude"], title):
        return None

    flair = post.flair
    if flair and any(bad in flair for bad in cfg.get("deny_flairs", [])):
        return None

    m = Match(post=post, price=parse_price(title))
    for rule in cfg.get("bonus", []):
        if re.search(rule["pattern"], title, re.I):
            m.score += int(rule.get("points", 1))
            m.reasons.append(rule["label"])

    cap = (cfg["target_price_desktop"] if m.kind == "desktop"
           else cfg["target_price_gpu"])
    if m.price is not None and m.price <= cap:
        m.hot = True
        m.score += 3
    # Only a price we actually read and that clears the cap disqualifies a
    # post. An unparseable price is not evidence of a bad deal.
    if cfg.get("hard_price_filter") and m.price is not None and not m.hot:
        return None
    return m




# ------------------------------------------------------------------- alerting

def notify_desktop(title: str, body: str, urgency: str = "normal") -> None:
    env = dict(os.environ)
    # systemd imports the session env once at install time; that snapshot goes
    # stale across logout/reboot, so prefer the live per-user bus socket.
    bus = Path(f"/run/user/{os.getuid()}/bus")
    if bus.exists():
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"
    env.setdefault("DISPLAY", ":0")
    try:
        subprocess.run(
            ["notify-send", "-u", urgency, "-t", "0", "-a", "dealwatch",
             "-i", "video-display", title, body],
            env=env, check=False, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


def notify_ntfy(cfg: dict, title: str, body: str, link: str, hot: bool) -> None:
    topic = cfg.get("ntfy_topic", "").strip()
    if not topic:
        return
    url = f"{cfg['ntfy_server'].rstrip('/')}/{urllib.parse.quote(topic)}"
    headers = {
        "Title": title.encode("ascii", "ignore").decode() or "buildapcsales hit",
        "Priority": "urgent" if hot else "high",
        "Tags": "fire" if hot else "computer",
        "Click": link,
        "Actions": f"view, Open post, {link}",
    }
    req = urllib.request.Request(url, data=body.encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"[warn] ntfy push failed: {e}", file=sys.stderr)


def telegram_creds(cfg: dict) -> tuple[str, str] | None:
    """Bot token + chat id from the environment, else the first env file
    that carries both. Same file daytrader uses, so one setup covers both."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if token and chat:
        return token, chat
    for name in cfg.get("telegram_env_files", []):
        path = Path(name).expanduser()
        if not path.exists():
            continue
        found = {}
        try:
            for line in path.read_text().splitlines():
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                found[key.strip()] = val.strip().strip("'\"")
        except OSError:
            continue
        if found.get("TELEGRAM_BOT_TOKEN") and found.get("TELEGRAM_CHAT_ID"):
            return found["TELEGRAM_BOT_TOKEN"], found["TELEGRAM_CHAT_ID"]
    return None


def notify_telegram(cfg: dict, title: str, body: str) -> bool:
    if not cfg.get("telegram", True):
        return False
    creds = telegram_creds(cfg)
    if not creds:
        return False
    token, chat = creds
    data = urllib.parse.urlencode({
        "chat_id": chat,
        # plain text, no parse_mode: deal titles are full of characters that
        # would 400 the API as markdown
        "text": f"{title}\n\n{body}",
        "disable_web_page_preview": "false",
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    try:
        urllib.request.urlopen(req, timeout=15).read()
        return True
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"[warn] telegram send failed: {e}", file=sys.stderr)
        return False


def log_hit(path: Path, m: Match) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "id": m.post.id, "title": m.post.title, "link": m.post.link,
        "price": m.price, "score": m.score, "hot": m.hot, "kind": m.kind,
    }
    with path.open("a") as f:
        f.write(json.dumps(rec) + "\n")


def alert(cfg: dict, m: Match, log_path: Path) -> None:
    price = f"${m.price:,.0f}" if m.price is not None else "price?"
    tags = " + ".join(m.reasons)
    flame = "HOT " if m.hot else ""
    head = f"{flame}5090 {m.kind.upper()} {price}"
    if tags:
        head += f" [{tags}]"
    body = f"{m.post.title}\n{m.post.link}"

    print(f"\n>>> {head}\n    {m.post.title}\n    {m.post.link}", flush=True)
    log_hit(log_path, m)
    if cfg.get("desktop_notify", True):
        notify_desktop(head, body, "critical" if m.hot else "normal")
    notify_ntfy(cfg, head, body, m.post.link, m.hot)
    notify_telegram(cfg, head, body)
    if m.hot and cfg.get("auto_open_hot"):
        subprocess.Popen(["xdg-open", m.post.link],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def setup_ntfy(config_path: Path, cfg: dict, log_path: Path) -> int:
    """Generate a private ntfy topic, save it, and push a test alert.

    Phone push matters more than the desktop toast here: good 5090s sell out
    while you are away from the machine.
    """
    local_path = config_path.with_name(config_path.stem + ".local" + config_path.suffix)
    topic = cfg.get("ntfy_topic", "").strip()
    if not topic:
        topic = f"5090-{secrets.token_urlsafe(9).replace('_', '-').lower()}"
        raw = json.loads(local_path.read_text()) if local_path.exists() else {}
        raw["ntfy_topic"] = topic
        local_path.write_text(json.dumps(raw, indent=2) + "\n")
        cfg["ntfy_topic"] = topic
        print(f"generated topic and saved to {local_path} (gitignored)")

    server = cfg["ntfy_server"].rstrip("/")
    print(f"""
  1. install the free "ntfy" app (App Store / Play Store / F-Droid)
  2. add a subscription to this topic:

         {topic}

     or open {server}/{topic} in a browser to watch it there
  3. keep the topic secret: anyone who knows it sees your alerts
""")
    notify_ntfy(cfg, "dealwatch is armed",
                "You will get a push the moment a 5090 hits r/buildapcsales.",
                f"{server}/{topic}", hot=False)
    print("test push sent. if it does not arrive, check the topic in the app.")
    return 0

# ----------------------------------------------------------------------- main

def run_once(cfg: dict, state_path: Path, log_path: Path,
             seed: bool = False, dry: bool = False) -> int:
    seen = load_seen(state_path)
    seen_set = set(seen)
    first_run = not state_path.exists()
    posts = parse_feed(fetch(feed_url(cfg), cfg["user_agent"]))
    hits, errors = 0, []
    for p in posts:
        if p.id in seen_set:
            continue
        try:
            m = evaluate(p, cfg)
        except Exception as e:
            # Do not mark it seen: an unevaluated post is not a judged post,
            # and silently dropping it forever is how a deal gets missed.
            # It is retried on the next poll, and the caller exits non-zero
            # so the failure is visible in systemctl status.
            errors.append(f"{p.id}: {e}")
            print(f"[error] could not evaluate {p.id}: {e}",
                  file=sys.stderr, flush=True)
            continue
        seen_set.add(p.id)
        seen.append(p.id)
        if not m:
            continue
        hits += 1
        if seed or first_run or dry:
            price = f"${m.price:,.0f}" if m.price else "price?"
            state = "seeded" if (seed or first_run) else "dry-run"
            print(f"[{state}] {price:>8}  {p.title}")
        else:
            try:
                alert(cfg, m, log_path)
            except Exception as e:
                # One broken alert must not cost the rest of the batch, and
                # must not leave this post unrecorded (it would re-alert).
                print(f"[error] alert failed for {p.id}: {e}",
                      file=sys.stderr, flush=True)
            finally:
                save_seen(state_path, seen)
    if not dry:
        save_seen(state_path, seen)
    print(f"[{time.strftime('%H:%M:%S')}] scanned {len(posts)} posts, "
          f"{hits} match(es)"
          + (f", {len(errors)} unevaluated (will retry)" if errors else "")
          + (" (first run: seeded, no alerts sent)" if first_run and not dry else ""),
          flush=True)
    if errors:
        raise RuntimeError(f"{len(errors)} post(s) could not be evaluated: "
                           + "; ".join(errors[:3]))
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(description="Alert on new 5090 posts in r/buildapcsales")
    ap.add_argument("--config", default=str(HOME / "config.json"))
    ap.add_argument("--state", default=str(DEFAULT_STATE))
    ap.add_argument("--log", default=str(DEFAULT_LOG))
    ap.add_argument("--once", action="store_true", help="single poll (cron/timer mode)")
    ap.add_argument("--interval", type=int, default=180, help="daemon poll seconds")
    ap.add_argument("--seed", action="store_true", help="mark current posts seen, no alerts")
    ap.add_argument("--dry-run", action="store_true", help="show matches, change nothing")
    ap.add_argument("--test-alert", action="store_true", help="fire a fake alert end to end")
    ap.add_argument("--setup-ntfy", action="store_true",
                    help="turn on phone push: make a private ntfy topic and test it")
    args = ap.parse_args()

    try:
        cfg = load_config(Path(args.config))
        validate_config(cfg)
    except (ValueError, json.JSONDecodeError) as e:
        print(f"[error] {args.config} is broken: {e}", file=sys.stderr)
        return 2
    state_path, log_path = Path(args.state), Path(args.log)

    if args.setup_ntfy:
        return setup_ntfy(Path(args.config), cfg, log_path)

    if args.test_alert:
        fake = Post(id="test", title="[GPU] ASUS TUF RTX 5090 OC - $1899.99 (Newegg)",
                    link="https://www.reddit.com/r/buildapcsales/")
        m = evaluate(fake, cfg)
        if not m:
            print("test post did not match config; check must_match/exclude")
            return 1
        alert(cfg, m, log_path)
        channels = []
        if cfg.get("desktop_notify", True) and shutil.which("notify-send"):
            channels.append("desktop")
        if cfg.get("ntfy_topic"):
            channels.append("ntfy")
        if telegram_creds(cfg):
            channels.append("telegram")
        print(f"test alert sent via: {', '.join(channels) or 'NOTHING - no channel configured'}")
        return 0 if channels else 1

    if args.once or args.seed or args.dry_run:
        try:
            run_once(cfg, state_path, log_path, seed=args.seed, dry=args.dry_run)
        except Exception as e:
            # Non-zero so `systemctl --user status dealwatch` shows the break
            # instead of the timer quietly doing nothing for days.
            print(f"[error] poll failed: {e}", file=sys.stderr, flush=True)
            return 1
        return 0

    print(f"watching r/{cfg['subreddit']} every {args.interval}s (ctrl-c to stop)")
    while True:
        try:
            run_once(cfg, state_path, log_path)
        except Exception as e:  # keep the daemon alive through transient failures
            print(f"[error] {e}", file=sys.stderr, flush=True)

        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
