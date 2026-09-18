"""Alert channels: desktop toast, ntfy push, Telegram.

Telegram is the one every watcher uses; the bot token and chat id come from
the environment or the first env file listed in cfg["telegram_env_files"]
(daytrader writes one, so one setup covers every project on the box).
"""

from __future__ import annotations

import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def money(value: float | None, symbol: str = "$") -> str:
    return f"{symbol}{value:,.0f}" if value is not None else "price?"


def notify_desktop(title: str, body: str, urgency: str = "normal",
                   app: str = "dealwatch") -> None:
    env = dict(os.environ)
    # systemd imports the session env once at install time; that snapshot goes
    # stale across logout/reboot, so prefer the live per-user bus socket.
    bus = Path(f"/run/user/{os.getuid()}/bus")
    if bus.exists():
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"
    env.setdefault("DISPLAY", ":0")
    try:
        subprocess.run(
            ["notify-send", "-u", urgency, "-t", "0", "-a", app,
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
    that carries both."""
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
