# dealwatch

Alerts you when a new RTX 5090 post lands in r/buildapcsales — either a bare GPU
or a prebuilt gaming desktop, with extra weight on 9950X3D / 9900X3D + 64GB rigs.

Pure stdlib Python, no dependencies, no reddit account needed.

## Quick start

```bash
cd ~/git/dealwatch
python3 dealwatch.py --dry-run     # see what currently matches, change nothing
python3 dealwatch.py --test-alert  # verify notifications actually reach you
python3 dealwatch.py --setup-ntfy  # optional but recommended: phone push
./install.sh                       # systemd --user timer, polls every 3 min
```

The first real run seeds state silently (no backlog blast); after that you only
get alerts for genuinely new posts.

```bash
journalctl --user -u dealwatch.service -f          # watch it work
systemctl --user disable --now dealwatch.timer     # stop
cat state/hits.jsonl                               # every alert ever fired
```

## Phone alerts (recommended)

Good 5090 deals die in minutes, and a desktop toast only helps if you are at the
desk. `--setup-ntfy` generates a private random topic, writes it to
`config.json`, and sends a test push:

```bash
python3 dealwatch.py --setup-ntfy
```

Then install the free ntfy app (iOS/Android/F-Droid) and subscribe to the topic
it prints. Nothing leaves your machine until you run that command. Anyone who
knows the topic string can read your alerts, so keep it secret. Hot deals push
at `urgent` priority so they break through Do Not Disturb.

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
| `target_price_gpu` | 2200 | at or under this, a GPU is flagged HOT |
| `target_price_desktop` | 3800 | same for a prebuilt |
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
