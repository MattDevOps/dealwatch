#!/usr/bin/env python3
"""Unit tests for dealwatch matching. Run: python3 test_dealwatch.py"""
import json
import tempfile
import unittest
from pathlib import Path

import dealwatch
from dealwatch import (DEFAULT_CONFIG, Post, evaluate, load_seen, parse_feed,
                       parse_price, run_once, save_seen,
                       validate_config)

CFG = DEFAULT_CONFIG

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <author><name>/u/dealbot</name></author>
    <id>t3_abc123</id>
    <link href="https://www.reddit.com/r/buildapcsales/comments/abc123/x/"/>
    <title>[GPU] MSI RTX 5090 Suprim - $1,949.99 (was $2,199)</title>
    <updated>2026-08-23T09:00:00+00:00</updated>
  </entry>
  <entry>
    <author><name>/u/dealbot</name></author>
    <id>t3_def456</id>
    <link href="https://www.reddit.com/r/buildapcsales/comments/def456/y/"/>
    <title>[SSD] Samsung 990 Pro 2TB - $129.99</title>
    <updated>2026-08-23T09:01:00+00:00</updated>
  </entry>
</feed>"""


class TestParsing(unittest.TestCase):
    def test_parse_feed(self):
        posts = parse_feed(FEED)
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0].id, "t3_abc123")
        self.assertIn("5090", posts[0].title)
        self.assertTrue(posts[0].link.startswith("https://"))
        self.assertEqual(posts[0].flair, "gpu")

    def test_price_takes_asking_price_not_was_price(self):
        self.assertEqual(parse_price("[GPU] RTX 5090 - $1,949.99 (was $2,199)"), 1949.99)

    def test_price_ignores_small_amounts_first(self):
        self.assertEqual(parse_price("[GPU] 5090 $50 off, now $1899"), 1899.0)

    def test_price_none(self):
        self.assertIsNone(parse_price("[GPU] RTX 5090 in stock at MSRP"))


class TestMatching(unittest.TestCase):
    def ev(self, title):
        return evaluate(Post(id="x", title=title, link="l"), CFG)

    def test_gpu_matches(self):
        m = self.ev("[GPU] Gigabyte RTX 5090 Windforce - $2,099.99 (Newegg)")
        self.assertIsNotNone(m)
        self.assertEqual(m.kind, "gpu")
        self.assertTrue(m.hot)  # under the 2200 gpu target

    def test_expensive_gpu_still_alerts_but_not_hot(self):
        m = self.ev("[GPU] ASUS ROG Astral RTX 5090 - $3,899.99")
        self.assertIsNotNone(m)
        self.assertFalse(m.hot)

    def test_anything_under_3500_is_urgent(self):
        for t in ("[GPU] ASUS ROG Astral RTX 5090 - $3,299",
                  "[Prebuilt] Skytech 9950X3D RTX 5090 64GB - $3,450",
                  "[GPU] MSI RTX 5090 - $2,099"):
            self.assertTrue(self.ev(t).hot, t)

    def test_prebuilt_with_bonuses(self):
        m = self.ev("[Desktop] Skytech RTX 5090 / 9950X3D / 64GB DDR5 - $3,499")
        self.assertIsNotNone(m)
        self.assertEqual(m.kind, "desktop")
        self.assertIn("9950X3D", m.reasons)
        self.assertIn("64GB", m.reasons)
        self.assertTrue(m.hot)
        self.assertGreaterEqual(m.score, 8)

    def test_9900x3d_scored(self):
        m = self.ev("[Desktop] NZXT Player Three 9900X3D RTX 5090 64GB - $3,299")
        self.assertIn("9900X3D", m.reasons)

    def test_laptop_excluded(self):
        self.assertIsNone(self.ev("[Laptop] Razer Blade 18 RTX 5090 Mobile - $3,499"))

    def test_non_5090_ignored(self):
        self.assertIsNone(self.ev("[GPU] RTX 5080 Founders Edition - $999"))
        self.assertIsNone(self.ev("[SSD] Samsung 990 Pro 2TB - $129.99"))

    def test_5090_in_offtarget_category_ignored(self):
        self.assertIsNone(self.ev("[Monitor] Some 5090 bundle monitor - $299"))

    def test_hard_price_filter(self):
        cfg = dict(CFG, hard_price_filter=True)
        cheap = evaluate(Post("x", "[GPU] RTX 5090 - $1,999", "l"), cfg)
        dear = evaluate(Post("x", "[GPU] RTX 5090 - $4,299", "l"), cfg)
        self.assertIsNotNone(cheap)
        self.assertIsNone(dear)

    def test_model_number_boundary(self):
        # should not fire on a part number that merely contains 5090
        self.assertIsNone(self.ev("[PSU] Corsair RM1000x model 15090x - $129"))



class TestFlairs(unittest.TestCase):
    def test_hyphenated_prebuilt_normalises(self):
        p = Post(id="x", title="[Pre-Built] iBUYPOWER RTX 5090 9950X3D 64GB - $3,499", link="l")
        self.assertEqual(p.flair, "prebuilt")
        m = evaluate(p, CFG)
        self.assertEqual(m.kind, "desktop")
        self.assertTrue(m.hot)

    def test_just_over_the_line_still_alerts_quietly(self):
        m = evaluate(Post("x", "[Pre-Built] iBUYPOWER RTX 5090 9950X3D 64GB - $3,599", "l"), CFG)
        self.assertIsNotNone(m, "over target is still worth seeing")
        self.assertFalse(m.hot, "but not urgent")

    def test_bundle_flair_is_a_rig_not_a_bare_card(self):
        # A full build posted under [Bundle] must get the desktop price cap,
        # not the (much lower) GPU one.
        m = evaluate(Post("x", "[Bundle] RTX 5090 + Ryzen 9950X3D + 64GB kit - $2,699 shipped", "l"), CFG)
        self.assertIsNotNone(m)
        self.assertEqual(m.kind, "desktop")
        self.assertTrue(m.hot, "$2699 rig is under the $3800 desktop target")
        self.assertIn("9950X3D", m.reasons)

    def test_bundle_rig_survives_hard_price_filter(self):
        cfg = dict(CFG, hard_price_filter=True)
        m = evaluate(Post("x", "[Bundle] RTX 5090 + 9950X3D + 64GB combo - $2,999", "l"), cfg)
        self.assertIsNotNone(m, "a $2999 5090 rig must never be filtered out")

    def test_cpu_flaired_rig_gets_desktop_cap(self):
        m = evaluate(Post("x", "[CPU] 9900X3D + RTX 5090 build - $3,299", "l"), CFG)
        self.assertEqual(m.kind, "desktop")
        self.assertTrue(m.hot)

    def test_denied_flairs(self):
        for t in ("[Monitor] 5090 rig monitor - $299",
                  "[PSU] 1200W for your 5090 - $199",
                  "[Cooler] AIO for 5090 builds - $89"):
            self.assertIsNone(evaluate(Post("x", t, "l"), CFG), t)

class TestPriceOrdering(unittest.TestCase):
    """parse_price must find what you pay, not the strikethrough or rebate."""

    def test_original_price_stated_first(self):
        self.assertEqual(
            parse_price("[GPU] RTX 5090 - originally $2499, drops to $1899 now (Amazon)"), 1899.0)

    def test_was_price_in_parens(self):
        self.assertEqual(parse_price("[GPU] RTX 5090 - $1,949.99 (was $2,199)"), 1949.99)

    def test_msrp_prefix(self):
        self.assertEqual(parse_price("[GPU] RTX 5090 MSRP $1999 - yours for $1,849"), 1849.0)

    def test_rebate_after_price(self):
        self.assertEqual(parse_price("[GPU] RTX 5090 - $1899 after $200 rebate"), 1899.0)

    def test_discount_amount_first(self):
        self.assertEqual(parse_price("[GPU] Save $300 on RTX 5090 - now $2,199"), 2199.0)

    def test_gift_card_suffix(self):
        self.assertEqual(parse_price("[GPU] RTX 5090 $2,099 + $150 gift card"), 2099.0)

    def test_hot_flag_uses_real_price(self):
        m = evaluate(Post("x", "[GPU] RTX 5090 - reg $2499, today $1,899", "l"), CFG)
        self.assertEqual(m.price, 1899.0)
        self.assertTrue(m.hot)


class TestSeenState(unittest.TestCase):
    def test_trim_keeps_the_newest_ids_in_order(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "seen.json"
            ids = [f"t3_{i:04d}" for i in range(50)]
            save_seen(path, ids, keep=10)
            kept = load_seen(path)
            self.assertEqual(kept, ids[-10:])  # newest, in order, deterministic

    def test_roundtrip_preserves_order(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "seen.json"
            ids = ["t3_c", "t3_a", "t3_b"]
            save_seen(path, ids)
            self.assertEqual(load_seen(path), ids)

    def test_missing_and_corrupt_state_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(load_seen(Path(d) / "nope.json"), [])
            bad = Path(d) / "bad.json"
            bad.write_text("{not json")
            self.assertEqual(load_seen(bad), [])

class TestPriceWordOrder(unittest.TestCase):
    """Noise words qualify the amount in their own clause, nothing further."""

    def check(self, title, expected):
        self.assertEqual(parse_price(title), expected, title)

    def test_discount_word_before_amount(self):
        self.check("[GPU] RTX 5090 list $2499, rebate $200", 2499.0)

    def test_retail_price_stated_first(self):
        self.check("[GPU] RTX 5090 $2,499.00 retail, but YMMV in cart for $1,999.00", 1999.0)

    def test_discount_word_binds_to_the_amount_it_follows(self):
        self.check("[GPU] 5090 $50 off, now $1899", 1899.0)
        self.check("[GPU] ASUS RTX 5090 - $2199.99 ($100 off w/ code SAVE100)", 2199.99)

    def test_only_was_prices_present_falls_back_to_the_item(self):
        # "$300 off" is a discount and can never stand in for the price;
        # the was-price at least has the right magnitude.
        self.check("[GPU] RTX 5090 - was $2499, $300 off", 2499.0)

    def test_normally_in_trailing_parens(self):
        self.check("[Prebuilt] Lenovo 5090/9950X3D/64GB $3,499 (normally $3,999)", 3499.0)


class TestRigDetection(unittest.TestCase):
    def kind_of(self, title):
        return evaluate(Post("x", title, "l"), CFG).kind

    def test_generic_rig_wording_without_a_named_cpu(self):
        self.assertEqual(self.kind_of("[Bundle] RTX 5090 rig with premium case - $2999"), "desktop")
        self.assertEqual(self.kind_of("[CPU] Full custom rig w/ RTX 5090 - $2999"), "desktop")

    def test_generic_rig_survives_hard_price_filter(self):
        cfg = dict(CFG, hard_price_filter=True)
        for t in ("[Bundle] RTX 5090 rig with premium case - $2999",
                  "[CPU] Full custom rig w/ RTX 5090 - $2999"):
            self.assertIsNotNone(evaluate(Post("x", t, "l"), cfg), t)

    def test_bare_card_stays_a_gpu(self):
        self.assertEqual(self.kind_of("[GPU] ASUS TUF RTX 5090 OC - $1899.99"), "gpu")
        self.assertEqual(self.kind_of("[GPU] MSI RTX 5090 Suprim, watercooling system ready - $2,150"), "gpu")

    def test_unknown_price_is_never_filtered_out(self):
        cfg = dict(CFG, hard_price_filter=True)
        m = evaluate(Post("x", "[GPU] RTX 5090 FE in stock at MSRP", "l"), cfg)
        self.assertIsNotNone(m, "an unreadable price is not evidence of a bad deal")
        self.assertIsNone(m.price)


class TestRunOnceStatePersistence(unittest.TestCase):
    """A post that has been alerted on must never be alerted on twice."""

    def test_alert_failure_mid_batch_does_not_cause_a_re_alert(self):
        feed = FEED.replace("[SSD] Samsung 990 Pro 2TB - $129.99",
                            "[GPU] EVGA RTX 5090 - $2,050")
        alerted = []

        def flaky_alert(cfg, m, log_path):
            alerted.append(m.post.id)
            if len(alerted) == 2:
                raise RuntimeError("ntfy exploded")

        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "seen.json"
            state.write_text(json.dumps({"seen": ["t3_old"]}))  # not a first run
            orig_fetch, orig_alert = dealwatch.fetch, dealwatch.alert
            dealwatch.fetch = lambda *a, **k: feed
            dealwatch.alert = flaky_alert
            try:
                run_once(CFG, state, Path(d) / "hits.jsonl")
                self.assertEqual(alerted, ["t3_abc123", "t3_def456"])
                run_once(CFG, state, Path(d) / "hits.jsonl")  # second poll
            finally:
                dealwatch.fetch, dealwatch.alert = orig_fetch, orig_alert
            self.assertEqual(alerted, ["t3_abc123", "t3_def456"],
                             "no post may alert twice across polls")
            self.assertEqual(set(load_seen(state)), {"t3_old", "t3_abc123", "t3_def456"})

class TestRigWordPrecision(unittest.TestCase):
    """"build"/"combo" in marketing copy must not promote a bare card."""

    def kind_of(self, title):
        return evaluate(Post("x", title, "l"), CFG).kind

    def test_marketing_copy_does_not_make_a_rig(self):
        for t in ("[GPU] Gigabyte RTX 5090 Windforce OC, excellent build quality - $2099",
                  "[GPU] MSI RTX 5090 solid build - $2199",
                  "[GPU] PNY RTX 5090 XLR8 Combo Pack (card + backplate) - $2099"):
            self.assertEqual(self.kind_of(t), "gpu", t)

    def test_real_rig_wording_still_counts(self):
        for t in ("[Bundle] Custom build with RTX 5090 and 64GB - $3,199",
                  "[Bundle] RTX 5090 rig with premium case - $2999",
                  "[CPU] Full custom rig w/ RTX 5090 - $2999",
                  "[Prebuilt] Skytech 9900X3D RTX 5090 64GB - $3,299"):
            self.assertEqual(self.kind_of(t), "desktop", t)


class TestBundleTotals(unittest.TestCase):
    def test_component_prices_yield_to_the_stated_total(self):
        self.assertEqual(
            parse_price("[Bundle] RTX 5090 ($1999) + Ryzen 9950X3D ($699) + 64GB RAM ($199) = $2897 total"),
            2897.0)

    def test_all_in_price(self):
        self.assertEqual(parse_price("[Prebuilt] 5090 rig - $3,199 all in"), 3199.0)

    def test_out_the_door(self):
        self.assertEqual(parse_price("[Prebuilt] 9950X3D/5090/64GB $3,450 out the door"), 3450.0)

    def test_shipped_is_not_a_total_keyword(self):
        self.assertEqual(parse_price("[GPU] RTX 5090 - $1899 shipped"), 1899.0)


class TestPollResilience(unittest.TestCase):
    """An unevaluated post is not a judged post: it must be retried, loudly."""

    def _run(self, cfg, state, log, evaluate_fn, alerted):
        real = dealwatch.evaluate
        orig_fetch, orig_alert = dealwatch.fetch, dealwatch.alert
        feed = FEED.replace("[SSD] Samsung 990 Pro 2TB - $129.99",
                            "[GPU] EVGA RTX 5090 - $2,050")
        dealwatch.fetch = lambda *a, **k: feed
        dealwatch.alert = lambda c, m, l: alerted.append(m.post.id)
        dealwatch.evaluate = lambda post, c: evaluate_fn(post, c, real)
        try:
            return run_once(cfg, state, log)
        finally:
            dealwatch.fetch, dealwatch.alert, dealwatch.evaluate = (
                orig_fetch, orig_alert, real)

    def test_broken_post_is_retried_after_the_config_is_fixed(self):
        alerted = []

        def broken(post, cfg, real):
            if post.id == "t3_abc123":
                raise ValueError("bad regex in must_match")
            return real(post, cfg)

        with tempfile.TemporaryDirectory() as d:
            state, log = Path(d) / "seen.json", Path(d) / "hits.jsonl"
            state.write_text(json.dumps({"seen": ["t3_old"]}))

            # Poll 1: one post cannot be judged. The other still alerts, the
            # broken one is NOT recorded, and the poll reports failure.
            with self.assertRaises(RuntimeError):
                self._run(CFG, state, log, broken, alerted)
            self.assertEqual(alerted, ["t3_def456"])
            self.assertNotIn("t3_abc123", load_seen(state),
                             "an unjudged post must not be marked seen")

            # Poll 2: config fixed. The post that was skipped now alerts.
            self._run(CFG, state, log, lambda p, c, real: real(p, c), alerted)
            self.assertEqual(alerted, ["t3_def456", "t3_abc123"],
                             "the skipped deal must not be lost forever")

    def test_healthy_poll_reports_success(self):
        alerted = []
        with tempfile.TemporaryDirectory() as d:
            state, log = Path(d) / "seen.json", Path(d) / "hits.jsonl"
            state.write_text(json.dumps({"seen": ["t3_old"]}))
            hits = self._run(CFG, state, log, lambda p, c, real: real(p, c), alerted)
            self.assertEqual(hits, 2)
            self.assertEqual(sorted(alerted), ["t3_abc123", "t3_def456"])


class TestConfigValidation(unittest.TestCase):
    def test_good_config_passes(self):
        validate_config(DEFAULT_CONFIG)

    def test_bad_must_match_regex_is_rejected(self):
        with self.assertRaises(ValueError) as cm:
            validate_config(dict(DEFAULT_CONFIG, must_match=[r"5090("]))
        self.assertIn("must_match", str(cm.exception))

    def test_bad_bonus_rule_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_config(dict(DEFAULT_CONFIG, bonus=[{"label": "x", "pattern": "["}]))
        with self.assertRaises(ValueError):
            validate_config(dict(DEFAULT_CONFIG, bonus=[{"pattern": "x"}]))


class TestMarketingCopy(unittest.TestCase):
    def test_total_value_is_not_the_price(self):
        t = "[Bundle] RTX 5090 + 9950X3D + 64GB, $4999 total value, today only $2899 shipped"
        self.assertEqual(parse_price(t), 2899.0)
        m = evaluate(Post("x", t, "l"), dict(CFG, hard_price_filter=True))
        self.assertIsNotNone(m, "a $2899 rig must survive the hard filter")
        self.assertTrue(m.hot)

    def test_total_savings_is_not_the_price(self):
        self.assertEqual(
            parse_price("[GPU] RTX 5090 - $2,099 (total savings of $300)"), 2099.0)

    def test_genuine_total_still_wins(self):
        self.assertEqual(
            parse_price("[Bundle] 5090 ($1999) + 9950X3D ($699) = $2897 total"), 2897.0)


class TestComponentRigDetection(unittest.TestCase):
    """A title listing parts is a machine, however it words it."""

    def kind_of(self, title):
        return evaluate(Post("x", title, "l"), CFG).kind

    def test_setup_without_a_named_cpu(self):
        self.assertEqual(self.kind_of(
            "[Bundle] Complete Gaming Setup: RTX 5090, 64GB RAM, 2TB NVMe - $2999"), "desktop")

    def test_gpu_plus_motherboard(self):
        self.assertEqual(self.kind_of("[Bundle] RTX 5090 + Motherboard bundle - $2799"), "desktop")

    def test_those_rigs_survive_the_hard_filter(self):
        cfg = dict(CFG, hard_price_filter=True)
        for t in ("[Bundle] Complete Gaming Setup: RTX 5090, 64GB RAM, 2TB NVMe - $2999",
                  "[Bundle] RTX 5090 + Motherboard bundle - $2799"):
            self.assertIsNotNone(evaluate(Post("x", t, "l"), cfg), t)

    def test_card_vram_is_not_system_ram(self):
        self.assertEqual(self.kind_of("[GPU] RTX 5090 FE 32GB GDDR7 - $1,999"), "gpu")

    def test_the_word_case_in_prose_is_not_a_pc_case(self):
        self.assertEqual(self.kind_of(
            "[GPU] RTX 5090 - $1999, in case anyone is interested"), "gpu")

    def test_a_real_case_still_counts(self):
        self.assertEqual(self.kind_of(
            "[Bundle] RTX 5090 + mid-tower chassis - $2,450"), "desktop")

    def test_plain_card_listings_stay_gpu(self):
        for t in ("[GPU] ASUS TUF RTX 5090 OC - $1899.99",
                  "[GPU] Gigabyte RTX 5090 Windforce, excellent build quality - $2099"):
            self.assertEqual(self.kind_of(t), "gpu", t)

class TestWatchdog(unittest.TestCase):
    """Silence must mean "no 5090s posted", never "it died last Tuesday"."""

    def setUp(self):
        self.sent = []
        self.orig = dealwatch.notify_telegram
        dealwatch.notify_telegram = lambda cfg, t, b: (self.sent.append((t, b)) or True)

    def tearDown(self):
        dealwatch.notify_telegram = self.orig

    def beat_file(self, d, **fields):
        path = Path(d) / "heartbeat.json"
        path.write_text(json.dumps(fields))
        return path

    def test_stale_poller_raises_the_alarm(self):
        import time as _t
        with tempfile.TemporaryDirectory() as d:
            path = self.beat_file(d, last_ok=_t.time() - 3 * 3600, polls=40)
            self.assertEqual(dealwatch.watchdog(DEFAULT_CONFIG, path), 1)
            self.assertIn("stopped polling", self.sent[0][0])

    def test_it_does_not_spam_the_same_alarm(self):
        import time as _t
        with tempfile.TemporaryDirectory() as d:
            path = self.beat_file(d, last_ok=_t.time() - 3 * 3600)
            dealwatch.watchdog(DEFAULT_CONFIG, path)
            self.sent.clear()
            dealwatch.watchdog(DEFAULT_CONFIG, path)
            self.assertEqual(self.sent, [], "one warning an hour, not one an hour x60")

    def test_healthy_poller_is_quiet_between_heartbeats(self):
        import time as _t
        now = _t.time()
        with tempfile.TemporaryDirectory() as d:
            path = self.beat_file(d, last_ok=now - 60, last_beat=now - 3600)
            self.assertEqual(dealwatch.watchdog(DEFAULT_CONFIG, path), 0)
            self.assertEqual(self.sent, [])

    def test_daily_heartbeat_fires_and_resets_counters(self):
        import time as _t
        now = _t.time()
        with tempfile.TemporaryDirectory() as d:
            path = self.beat_file(d, last_ok=now - 60, last_beat=now - 25 * 3600,
                                  polls=400, hits=2)
            dealwatch.watchdog(DEFAULT_CONFIG, path)
            self.assertIn("alive", self.sent[0][0])
            self.assertIn("400 polls", self.sent[0][1])
            beat = json.loads(path.read_text())
            self.assertEqual((beat["polls"], beat["hits"]), (0, 0))

    def test_no_poll_yet_is_not_an_alarm(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(dealwatch.watchdog(DEFAULT_CONFIG, Path(d) / "none.json"), 0)
            self.assertEqual(self.sent, [])

    def test_a_successful_poll_stamps_the_heartbeat(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "heartbeat.json"
            dealwatch.record_poll(path, scanned=50, hits=1)
            dealwatch.record_poll(path, scanned=50, hits=0)
            beat = json.loads(path.read_text())
            self.assertEqual((beat["polls"], beat["hits"], beat["scanned"]), (2, 1, 50))
            self.assertGreater(beat["last_ok"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
