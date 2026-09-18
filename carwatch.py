#!/usr/bin/env python3
"""carwatch - alert on new facelifted Ioniq 5 listings in Israel.

Sources: yad2 (private + dealer ads, behind a bot wall, needs Chrome) and
carwiz (dealer inventory, plain HTTP). A listing is alerted at most once,
and only after its own page was fetched at alert time: the link is live,
the ad is not expired, and the car's power/battery/trim say facelift. The
alert lists that evidence, or says UNVERIFIED when the ad shows none.

Shares the Telegram channel, config layering and watchdog with dealwatch.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import src_carwiz
import src_yad2
from listings import Listing, Source, Verdict, classify
from notify import money, notify_telegram, telegram_creds
from state import (append_jsonl, load_config, load_seen, record_poll,
                   save_seen, watchdog)

HOME = Path(__file__).resolve().parent
STATE_DIR = HOME / "state"
SEEN_PATH = STATE_DIR / "carwatch-seen.json"
LOG_PATH = STATE_DIR / "carwatch-hits.jsonl"
BEAT_PATH = STATE_DIR / "carwatch-heartbeat.json"
SHEKEL = "\u20aa"
DEFAULT_CONFIG = {
    "sources": ["yad2", "carwiz"],
    "user_agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"),
    "telegram": True,
    "telegram_env_files": ["~/.config/dealwatch.env", "~/.config/daytrader.env"],
    "watch_label": "yad2 + carwiz for a facelift Ioniq 5",
    "stale_minutes": 45,
    "heartbeat_hours": 0,
    **src_yad2.DEFAULTS,
    **src_carwiz.DEFAULTS,
}
SOURCES: dict[str, type[Source]] = {"yad2": src_yad2.Source, "carwiz": src_carwiz.Source}


# ------------------------------------------------------------------ messages

def describe(lst: Listing) -> str:
    """The lines under the headline: car, seller, then the checks."""
    car = [lst.title]
    if lst.km is not None:
        car.append(f"{lst.km:,} km")
    car += [x for x in (lst.hand, lst.city) if x]
    who = [f"seller: {lst.seller or '?'}"]
    if lst.created_at:
        who.append(f"listed {lst.created_at}")
    who += lst.tags
    return " · ".join(car) + "\n" + " · ".join(who)


def headline(lst: Listing, verdict: Verdict) -> str:
    head = f"Ioniq 5 facelift on {lst.source}: {money(lst.price, SHEKEL)}"
    if verdict.status == "unverified":
        head = "UNVERIFIED " + head
    return head


def message(lst: Listing, verdict: Verdict) -> str:
    return (f"{describe(lst)}\nChecked: {'; '.join(verdict.evidence)}\n"
            f"{lst.url}")


def log_hit(path: Path, lst: Listing, verdict: Verdict) -> None:
    append_jsonl(path, {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "source": lst.source,
        "id": lst.id, "url": lst.url, "title": lst.title, "price": lst.price,
        "status": verdict.status, "evidence": verdict.evidence})


def alert(cfg: dict, lst: Listing, verdict: Verdict, log_path: Path) -> None:
    head, body = headline(lst, verdict), message(lst, verdict)
    print(f"\n>>> {head}\n" + "\n".join("    " + l for l in body.splitlines()),
          flush=True)
    log_hit(log_path, lst, verdict)
    notify_telegram(cfg, head, body)


def digest(cfg: dict, found: list[tuple[Listing, Verdict]]) -> None:
    """First run: one message with everything live right now, so the seed
    is not silent but also not twenty pings."""
    if not found:
        notify_telegram(cfg, "carwatch is armed",
                        "No facelift Ioniq 5 listed right now on yad2 or "
                        "carwiz. You get a ping when one appears.")
        return
    parts = [f"{headline(l, v)}\n{message(l, v)}" for l, v in found]
    notify_telegram(cfg, f"carwatch is armed: {len(found)} listed right now",
                    "\n\n".join(parts)[:3900])


# ---------------------------------------------------------------------- poll

def _seen_key(name: str, lst: Listing) -> str:
    return f"{name}:{lst.id}"


def judge(src, cand: Listing) -> tuple[Listing | None, Verdict | None]:
    """Fetch the ad's own page and classify. (None, None) = gone."""
    full = src.verify(cand)
    if full is None:
        return None, None
    return full, classify(full)


def poll_source(name: str, cfg: dict, seen: list[str],
                on_hit) -> tuple[int, int, list[str]]:
    """One source, one poll: (candidates, hits, errors).

    Only a judged listing is marked seen; a fetch that failed is retried
    next poll, so a flaky page can never make a car vanish for good.
    """
    seen_set = set(seen)
    hits, errors = 0, []
    with SOURCES[name](cfg, STATE_DIR) as src:
        cands = src.search()
        print(f"[{name}] {len(cands)} listing(s) in search", flush=True)
        for cand in cands:
            key = _seen_key(name, cand)
            if key in seen_set:
                continue
            try:
                full, verdict = judge(src, cand)
            except Exception as e:
                errors.append(f"{key}: {e}")
                print(f"[error] could not check {key}: {e}", file=sys.stderr, flush=True)
                continue
            seen_set.add(key)
            seen.append(key)
            if full is None:
                print(f"  [gone] {cand.title}  {cand.url}")
                continue
            status = verdict.status
            print(f"  [{status}] {full.title:<28} {money(full.price, SHEKEL):>10}  "
                  f"{'; '.join(verdict.evidence)}")
            if verdict.wanted:
                hits += 1
                on_hit(full, verdict)
    return len(cands), hits, errors


def run_once(cfg: dict, state_path: Path, log_path: Path, beat_path: Path,
             dry: bool = False) -> int:
    """Poll every source. The first run (no state file yet) judges what is
    listed right now and sends one digest instead of an alert per car."""
    seen = load_seen(state_path)
    first_run = not state_path.exists()
    found: list[tuple[Listing, Verdict]] = []

    def on_hit(lst, verdict):
        if dry:
            return
        if first_run:
            found.append((lst, verdict))
            log_hit(log_path, lst, verdict)
        else:
            try:
                alert(cfg, lst, verdict, log_path)
            except Exception as e:
                print(f"[error] alert failed for {lst.url}: {e}",
                      file=sys.stderr, flush=True)

    scanned, hits, errors = 0, 0, []
    for name in cfg["sources"]:
        try:
            n, h, errs = poll_source(name, cfg, seen, on_hit=on_hit)
        except Exception as e:
            # One source down (bot wall, outage) must not stop the other.
            errors.append(f"{name}: {e}")
            print(f"[error] {name} poll failed: {e}", file=sys.stderr, flush=True)
            continue
        scanned, hits = scanned + n, hits + h
        errors += errs
        if not dry:
            save_seen(state_path, seen)
    if first_run and not dry:
        digest(cfg, found)
    print(f"[{time.strftime('%H:%M:%S')}] {scanned} listing(s) scanned, "
          f"{hits} wanted"
          + (f", {len(errors)} unchecked (will retry)" if errors else "")
          + (" (first run: digest sent instead of alerts)" if first_run and not dry else ""),
          flush=True)
    if errors:
        raise RuntimeError(f"{len(errors)} problem(s): " + "; ".join(errors[:3]))
    if not dry:
        record_poll(beat_path, scanned, hits)
    return hits


def seed_all(cfg: dict, state_path: Path) -> None:
    """Mark everything currently listed as seen without judging it."""
    seen = load_seen(state_path)
    seen_set = set(seen)
    for name in cfg["sources"]:
        with SOURCES[name](cfg, STATE_DIR) as src:
            for cand in src.search():
                key = _seen_key(name, cand)
                if key not in seen_set:
                    seen_set.add(key)
                    seen.append(key)
                    print(f"[seeded] {name} {cand.title:<28} {cand.url}")
    save_seen(state_path, seen)


def test_alert(cfg: dict, log_path: Path) -> int:
    fake = Listing(source="yad2", id="test", url="https://www.yad2.co.il/vehicles/cars",
                   year=2025, trim="Limited", hp=229, price=189000, km=1200,
                   hand="יד ראשונה", city="תל אביב", seller="private",
                   created_at="2026-09-18", description="פייסליפט 84 קוט״ש מגב אחורי")
    alert(cfg, fake, classify(fake), log_path)
    ok = telegram_creds(cfg) is not None
    print("test alert sent via telegram" if ok else
          "NO telegram channel: set TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID in "
          + ", ".join(cfg["telegram_env_files"]))
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Alert on new facelift Ioniq 5 listings")
    ap.add_argument("--config", default=str(HOME / "carwatch.json"))
    ap.add_argument("--state", default=str(SEEN_PATH))
    ap.add_argument("--log", default=str(LOG_PATH))
    ap.add_argument("--beat", default=str(BEAT_PATH))
    ap.add_argument("--once", action="store_true",
                    help="single poll (the default; kept for timer units)")
    ap.add_argument("--seed", action="store_true",
                    help="mark everything currently listed as seen, no checks, no alerts")
    ap.add_argument("--dry-run", action="store_true",
                    help="check and print every unseen listing, change nothing")
    ap.add_argument("--test-alert", action="store_true", help="send a fake alert")
    ap.add_argument("--watchdog", action="store_true",
                    help="warn over Telegram if polling has stopped")
    ap.add_argument("--source", choices=sorted(SOURCES), help="poll one source only")
    args = ap.parse_args()

    cfg = load_config(Path(args.config), DEFAULT_CONFIG)
    if args.source:
        cfg["sources"] = [args.source]
    state_path, log_path, beat_path = Path(args.state), Path(args.log), Path(args.beat)

    if args.watchdog:
        return watchdog(cfg, beat_path, name="carwatch", label=cfg["watch_label"])
    if args.test_alert:
        return test_alert(cfg, log_path)
    if args.seed:
        seed_all(cfg, state_path)
        return 0
    try:
        run_once(cfg, state_path, log_path, beat_path, dry=args.dry_run)
    except Exception as e:
        # Non-zero so `systemctl --user status carwatch` shows the break.
        print(f"[error] poll failed: {e}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
