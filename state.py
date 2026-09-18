"""What a watcher keeps on disk, and the watchdog that reads it.

config: defaults, then <name>.json, then <name>.local.json (gitignored).
seen:   ids already judged, oldest first, trimmed.
beat:   when the last poll succeeded and what it found, for the watchdog.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from notify import money, notify_telegram

# How many matched posts the heartbeat carries: kept in the state file, and
# shown in the message. Telegram caps a message at 4096 characters.
RECENT_KEEP = 25
RECENT_SHOW = 15
WARN_EVERY_SECONDS = 3600   # a stopped poller is reported at most hourly


def load_config(path: Path, defaults: dict) -> dict:
    """defaults, then path, then path's .local twin.

    The .local file is gitignored and is where anything private belongs --
    the ntfy topic is a shared secret, so it must never reach a public repo.
    """
    cfg = json.loads(json.dumps(defaults))
    for layer in (path, path.with_name(path.stem + ".local" + path.suffix)):
        if layer.exists():
            for k, v in json.loads(layer.read_text()).items():
                cfg[k] = v
    return cfg


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj))
    tmp.replace(path)


def append_jsonl(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ----------------------------------------------------------------------- seen

def load_seen(path: Path) -> list[str]:
    """Ids already judged, oldest first. A list, not a set: the trim in
    save_seen drops the oldest, and set iteration order would make that
    arbitrary."""
    if not path.exists():
        return []
    try:
        return list(json.loads(path.read_text()).get("seen", []))
    except (json.JSONDecodeError, OSError):
        return []


def save_seen(path: Path, seen: list[str], keep: int = 3000) -> None:
    _write_json(path, {"seen": seen[-keep:]})


# ------------------------------------------------------------------ heartbeat

def load_beat(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError, FileNotFoundError):
        return {}


def save_beat(path: Path, beat: dict) -> None:
    _write_json(path, beat)


def record_poll(path: Path, scanned: int, hits: int,
                recent: "list[dict] | tuple" = ()) -> None:
    """Stamp a successful poll, and keep what it found.

    The heartbeat is only useful if it says *which* items matched, so they
    ride along in the state file (as {title, link, price, hot} dicts) until
    the next heartbeat ships them. Capped: a burst of hits must not grow
    this file without bound.
    """
    beat = load_beat(path)
    beat["last_ok"] = time.time()
    beat["polls"] = beat.get("polls", 0) + 1
    beat["hits"] = beat.get("hits", 0) + hits
    beat["scanned"] = scanned
    if recent:
        kept = list(beat.get("recent", []))
        kept.extend(recent)
        beat["recent"] = kept[-RECENT_KEEP:]
    save_beat(path, beat)


def format_recent(recent: list[dict], total: int) -> str:
    """The links, newest first, for the heartbeat body."""
    lines = []
    for r in reversed(recent[-RECENT_SHOW:]):
        flame = "HOT " if r.get("hot") else ""
        lines.append(f"{flame}{money(r.get('price'))}  {r.get('title', '')}\n"
                     f"{r.get('link', '')}")
    dropped = total - len(lines)
    if dropped > 0:
        lines.append(f"...and {dropped} more (see the log)")
    return "\n\n".join(lines)


def watchdog(cfg: dict, beat_path: Path, name: str, label: str) -> int:
    """Report a stopped poller, and confirm a working one on a schedule.

    Runs from its own timer so it still speaks up when the poller itself is
    the thing that is broken. `name` is the systemd unit; `label` says what
    is being watched, for the heartbeat body.
    """
    beat = load_beat(beat_path)
    now = time.time()
    last_ok = beat.get("last_ok", 0)
    stale_after = float(cfg["stale_minutes"]) * 60

    if not last_ok:
        print("no successful poll recorded yet")
        return 0

    quiet = now - last_ok
    if quiet > stale_after:
        mins = int(quiet // 60)
        if now - beat.get("last_warn", 0) > WARN_EVERY_SECONDS:
            notify_telegram(
                cfg, f"{name} has stopped polling",
                f"No successful poll for {mins} minutes "
                f"(expected one every few minutes).\n"
                f"Check: systemctl --user status {name}.timer")
            beat["last_warn"] = now
            save_beat(beat_path, beat)
        print(f"STALE: last successful poll {mins} minutes ago")
        return 1

    every = float(cfg["heartbeat_hours"]) * 3600
    if every > 0 and now - beat.get("last_beat", 0) > every:
        hours = int((now - beat.get("last_beat", now - every)) // 3600)
        hits = beat.get("hits", 0)
        recent = list(beat.get("recent", []))
        body = (f"{beat.get('polls', 0)} polls in the last {hours}h, "
                f"{hits} matching post(s). Watching {label}.")
        if recent:
            body += "\n\n" + format_recent(recent, max(hits, len(recent)))
        elif hits:
            # counted before this version started keeping the links
            body += "\n\nLinks were not recorded for these; see the log."
        sent = notify_telegram(cfg, f"{name} is alive", body)
        beat.update(last_beat=now, polls=0, hits=0, recent=[])
        save_beat(beat_path, beat)
        print("heartbeat sent" if sent else "heartbeat skipped: no telegram channel")
    else:
        print(f"ok: last poll {int(quiet // 60)}m ago")
    return 0
