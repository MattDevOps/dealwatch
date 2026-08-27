# dealwatch

Alerts you when a new RTX 5090 post lands in r/buildapcsales — either a bare GPU
or a prebuilt gaming desktop, with extra weight on 9950X3D / 9900X3D + 64GB rigs.

Pure stdlib Python, no dependencies, no reddit account needed.

## Quick start

```bash
python3 dealwatch.py --dry-run     # see what currently matches, change nothing
python3 dealwatch.py --test-alert  # verify notifications actually reach you
./install.sh                       # systemd --user timer, polls every 3 min
```

For a server instead of this machine, see [Running it on a
server](#running-it-on-a-server). For phone alerts, see [Phone
alerts](#phone-alerts).

The first real run seeds state silently (no backlog blast); after that you only
get alerts for genuinely new posts.

```bash
journalctl --user -u dealwatch.service -f          # watch it work
systemctl --user disable --now dealwatch.timer     # stop
cat state/hits.jsonl                               # every alert ever fired
```

## Running it on a server

A laptop that sleeps misses deals. `deploy-vps.sh` rsyncs the code to a box,
runs the test suite there, installs the same systemd timer, and enables linger
so it keeps polling with nobody logged in:

```bash
cp deploy.env.example deploy.env   # fill in user@host + key
./deploy-vps.sh --seed             # --seed on the first deploy only
```

Reddit serves the feed to datacenter IPs, so a cheap VPS works.

## Phone alerts

Good 5090 deals die in minutes, and a desktop toast only helps if you are at the
desk. Two options, both optional -- desktop notification works out of the box.

**Telegram** (nothing to install if you already have a bot): set
`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in the environment or in
`~/.config/dealwatch.env`. `~/.config/daytrader.env` is also read, so a box
already running that project needs no new setup. Get a token from @BotFather
and your chat id from `https://api.telegram.org/bot<TOKEN>/getUpdates`.

**ntfy** (no account, but you install an app): `--setup-ntfy` generates a
private random topic, writes it to the gitignored `config.local.json`, and
sends a test push:

```bash
python3 dealwatch.py --setup-ntfy
```

Then install the free ntfy app (iOS/Android/F-Droid) and subscribe to the topic
it prints. Nothing leaves your machine until you run that command. Anyone who
knows the topic string can read your alerts, so keep it secret (which is why it
lands in `config.local.json`, never in git). Hot deals push at `urgent`
priority so they break through Do Not Disturb.

`--test-alert` prints which channels actually fired, so a misconfigured one is
visible immediately rather than the first time a deal shows up.

## Staying honest about silence

No alerts can mean "no 5090s posted" or "it died on Tuesday", and those look
identical from your phone. A second timer runs `--watchdog` hourly: it messages
you if no poll has succeeded in 30 minutes (at most one warning an hour). That
is the only routine message it sends: a working dealwatch is silent until a
deal shows up.

It can also confirm it is alive on a schedule, carrying the poll count *and*
every post that matched since the last one -- price, title and link, newest
first -- as a digest of what you may have missed (capped at 15 links; the rest
are counted and left in the log). That is **off** (`heartbeat_hours: 0`); set
it to `24` for a daily one. Every successful poll stamps
`state/heartbeat.json`, which holds those links until a heartbeat ships them.

## How it works

- Polls `https://www.reddit.com/r/buildapcsales/new/.rss` (the `.json` endpoint
  is now behind a browser interstitial; RSS is not).
- Reddit rate-limits anonymous RSS to roughly one request per 60s window and
  returns `x-ratelimit-reset` on a 429, which `fetch()` waits out exactly.
  A 3-minute poll interval sits well inside the limit.
- Dedupes on the reddit post id in `state/seen.json` (newest 3000, in order).
- Reads the price you actually pay: was-prices and rebate amounts
  ("$1,949 (was $2,199)", "$1899 after $200 rebate") are discarded.
- Treats a title listing any non-GPU part (CPU, mobo, RAM, storage, PSU, case)
  or rig wording as a whole system whatever its flair ([Bundle] and [CPU] posts are often full builds), so the desktop
  price cap applies rather than the bare-card one.
- Uses the stated total when a bundle title adds its parts up
  ("5090 ($1999) + 9950X3D ($699) = $2897 total").
- Commits dedupe state per alert, so a failed push never re-alerts you later.
- Never marks a post seen unless it was actually judged: if anything goes wrong
  evaluating one, the rest of the poll continues, that post is retried next
  tick, and the run exits non-zero so `systemctl --user status dealwatch`
  shows it instead of the timer looking healthy while dropping deals.
- Validates every regex in `config.json` at startup and exits 2 on a typo,
  rather than silently matching nothing.

## Matching rules (`config.json`)

Anything in `config.json` overrides `DEFAULT_CONFIG` in `dealwatch.py`.

| key | default | meaning |
| --- | --- | --- |
| `must_match` | `\b(rtx\s*)?5090\b` | post must hit one of these to alert |
| `exclude` | laptop, mobile, handheld | kills the alert outright |
| `deny_flairs` | monitor, psu, cooler, … | flairs that never hold the deal |
| `bonus` | 9950X3D, 9900X3D, 64GB, FE | scored and shown as tags in the alert |
| `target_price_gpu` | 3500 | at or under this, a GPU is flagged HOT |
| `target_price_desktop` | 3500 | same for a prebuilt |
| `stale_minutes` | 30 | watchdog warns if polling stops for this long |
| `heartbeat_hours` | 0 | how often the watchdog confirms it is alive; 0 = never |
| `hard_price_filter` | `false` | `true` = drop posts priced above target |
| `auto_open_hot` | `false` | `true` = open hot deals in the browser |

`hard_price_filter` is off on purpose: an over-priced ping costs you two
seconds, a missed 5090 costs you the deal. Turn it on if the noise gets old --
it only drops posts whose price was read *and* is above target, so a title with
an unparseable price still alerts.

## Tuning

Widen to any high-end card:

```json
{ "must_match": ["\\b(rtx\\s*)?(5090|5080)\\b"] }
```

Only want full rigs with the CPU you actually want:

```json
{ "must_match": ["\\b(9950|9900)\\s*x3d\\b"] }
```

## Tests

```bash
python3 test_dealwatch.py
```
