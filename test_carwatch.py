#!/usr/bin/env python3
"""Unit tests for carwatch. Run: python3 test_carwatch.py

Site payloads are small hand-made dicts in the shape the sites serve
(captured 2026-09-18); no network, no Chrome.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import carwatch
import src_carwiz
import src_yad2
import state
from listings import Listing, classify


def L(**kw) -> Listing:
    base = dict(source="yad2", id="t")
    base.update(kw)
    base.setdefault("url", f"https://www.yad2.co.il/vehicles/item/{base['id']}")
    return Listing(**base)


class TestClassify(unittest.TestCase):
    def test_facelift_power_confirms(self):
        v = classify(L(year=2025, trim="Limited", hp=229))
        self.assertEqual(v.status, "confirmed")
        self.assertIn("229hp", v.evidence[0])

    def test_awd_facelift_power_confirms(self):
        self.assertEqual(classify(L(year=2024, trim="Elite", hp=325)).status, "confirmed")

    def test_prefacelift_power_rejects_even_when_listed_as_2025(self):
        v = classify(L(year=2025, trim="Elite", hp=217))
        self.assertEqual(v.status, "rejected")

    def test_battery_decides(self):
        self.assertEqual(classify(L(year=2024, trim="Luxury", battery_kwh=72.6)).status, "rejected")
        self.assertEqual(classify(L(year=2024, trim="Elite", battery_kwh=84)).status, "confirmed")

    def test_ioniq6_shares_the_motor_rating_and_is_still_rejected(self):
        v = classify(L(source="carwiz", model="ioniq 6? (body סדאן)", year=2024,
                       trim="ELITE", hp=228, battery_kwh=77.4))
        self.assertEqual(v.status, "rejected")
        self.assertEqual(len(v.evidence), 1)

    def test_2023_is_never_a_facelift(self):
        self.assertEqual(classify(L(year=2023, trim="Limited", hp=229)).status, "rejected")

    def test_2025_without_hard_data_is_unverified(self):
        v = classify(L(year=2025, trim="Elite"))
        self.assertEqual(v.status, "unverified")
        self.assertTrue(v.wanted)

    def test_2024_trim_only(self):
        self.assertEqual(classify(L(year=2024, trim="Limited")).status, "confirmed")
        self.assertEqual(classify(L(year=2024, trim="Long Range Mid")).status, "confirmed")
        self.assertEqual(classify(L(year=2024, trim="4X2 Luxury")).status, "rejected")
        self.assertEqual(classify(L(year=2024, trim="Prestige")).status, "rejected")
        self.assertEqual(classify(L(year=2024, trim="Elite")).status, "unverified")

    def test_seller_text_counts(self):
        v = classify(L(year=2024, trim="Elite", description="פייסליפט 84 קוט״ש, מגב אחורי"))
        self.assertEqual(v.status, "confirmed")
        self.assertTrue(any("rear wiper" in e for e in v.evidence))

    def test_contradiction_is_flagged_not_dropped(self):
        v = classify(L(year=2024, trim="Elite", hp=217, description="84 kWh facelift"))
        self.assertEqual(v.status, "unverified")
        self.assertTrue(v.wanted)


# --------------------------------------------------------------------- yad2

def yad2_item(token, year, sub, price=150000, **extra):
    item = {"token": token, "adType": "private", "price": price,
            "address": {"city": {"text": "חיפה"}, "area": {"text": "צפון"}},
            "vehicleDates": {"yearOfProduction": year},
            "subModel": {"text": sub}, "hand": {"text": "יד ראשונה"},
            "createdAt": "2026-09-17T14:06:43", "customer": {}}
    item.update(extra)
    return item


def yad2_page(key, data):
    return {"dehydratedState": {"queries": [
        {"queryKey": key, "state": {"data": data}},
        {"queryKey": ["similar-links", "cars"], "state": {"data": []}},
    ]}}


class TestYad2Parsing(unittest.TestCase):
    def test_classic_feed_layout(self):
        page = yad2_page(["feed", "vehicles", "cars", {}], {
            "platinum": [yad2_item("aaa", 2025, "Limited חשמלי אוט׳ (229 כ״ס)",
                                   adType="commercial",
                                   customer={"agencyName": "כונס רכב "},
                                   externalKonesUrl="https://x")],
            "commercial": [yad2_item("aaa", 2025, "Limited חשמלי אוט׳ (229 כ״ס)")],
            "private": [yad2_item("bbb", 2024, "4X2 Luxury חשמלי אוט׳ (217 כ״ס)")],
            "pagination": {"pages": 1},
        })
        ls = src_yad2.parse_feed(page)
        self.assertEqual([l.id for l in ls], ["aaa", "bbb"])  # deduped
        a, b = ls
        self.assertEqual((a.trim, a.hp, a.seller, a.tags), ("Limited", 229, "כונס רכב", ["auction"]))
        self.assertEqual((b.trim, b.hp, b.city, b.created_at), ("4X2 Luxury", 217, "חיפה", "2026-09-17"))
        self.assertEqual(b.url, "https://www.yad2.co.il/vehicles/item/bbb")

    def test_feed_mix_layout(self):
        page = yad2_page(["feed-mix", "vehicles", "cars", {}], {
            "ads": [yad2_item("ccc", 2024, "Prestige חשמלי אוט׳ (217 כ״ס)")],
            "slotTypes": [{"clientType": "private"}], "platinum": [],
        })
        self.assertEqual([l.id for l in src_yad2.parse_feed(page)], ["ccc"])

    def test_item_page_fills_hard_data(self):
        lst = L(id="aaa", year=2025, trim="Limited", hp=None)
        page = yad2_page(["vehicles", "item", "aaa"], dict(
            yad2_item("aaa", 2025, "Limited חשמלי אוט׳ (229 כ״ס)"),
            horsePower=229, km=1200, allElectricRange="441 - 485",
            metaData={"description": "מגב אחורי"},
            dates={"endsAt": "2099-12-31T00:00:00"},
            vehicleDates={"yearOfProduction": 2025,
                          "monthOfProduction": {"text": "ינואר"}}))
        out = src_yad2.parse_item(lst, page)
        self.assertEqual((out.hp, out.km, out.range_km), (229, 1200, "441 - 485"))
        self.assertIn("ינואר", out.tags)
        self.assertEqual(classify(out).status, "confirmed")

    def test_missing_or_expired_item_is_gone(self):
        lst = L(id="zzz")
        self.assertIsNone(src_yad2.parse_item(lst, yad2_page(["user", "details"], {})))
        page = yad2_page(["vehicles", "item", "zzz"], dict(
            yad2_item("zzz", 2025, "Limited"), dates={"endsAt": "2020-01-01T00:00:00"}))
        self.assertIsNone(src_yad2.parse_item(lst, page))

    def test_search_url(self):
        self.assertEqual(
            src_yad2.search_url(carwatch.DEFAULT_CONFIG),
            "https://www.yad2.co.il/vehicles/cars?manufacturer=21&model=11239&year=2024-2026")


# ------------------------------------------------------------------- carwiz

def carwiz_post(car_id, year, trim, price=169900, **extra):
    p = {"carId": car_id, "finishLevel": trim, "price": price, "city": "פתח תקווה",
         "specs": [str(year), "יד 2", "13,000 ק״מ", "545 טווח נסיעה"],
         "link": f"/used-cars/{car_id}/{year}-x"}
    p.update(extra)
    return p


class TestCarwizParsing(unittest.TestCase):
    def test_search_skips_promos(self):
        page = {"resultsPage": {"posts": [
            carwiz_post("a1", 2024, "ELITE"),
            {"carId": "FUNDING_NEW", "isPromotion": True},
            {"title": None},
        ]}}
        ls = src_carwiz.parse_search(page)
        self.assertEqual(len(ls), 1)
        self.assertEqual((ls[0].year, ls[0].trim, ls[0].km, ls[0].range_km),
                         (2024, "ELITE", 13000, "545"))
        self.assertEqual(ls[0].url, "https://carwiz.co.il/used-cars/a1/2024-x")

    def test_search_url(self):
        self.assertEqual(
            src_carwiz.search_url(carwatch.DEFAULT_CONFIG),
            "https://carwiz.co.il/used-cars/%D7%99%D7%95%D7%A0%D7%93%D7%90%D7%99/"
            "%D7%90%D7%99%D7%95%D7%A0%D7%99%D7%A7/year-2024-2026")

    def detail(self, **spec):
        base = {"makeName": "יונדאי", "modelName": "איוניק", "year": 2024,
                "finishLevel": "ELITE", "fuelType": "חשמל", "body": "MPV",
                "drivingRange": 481, "maximumHorsePower": 228, "batteryCapacityKwh": 84}
        base.update(spec)
        return {"extendedPost": {"publish": True, "deletedAt": None, "priceV2": 169900,
                                 "kilometrage": 13000, "createdAt": "2026-08-25T13:55:23",
                                 "remark": "", "specification": base,
                                 "agencyBranch": {"city": "חיפה", "agency": {"displayName": "סמייל"}}}}

    def test_detail_fills_and_classifies(self):
        out = src_carwiz.parse_detail(L(source="carwiz", id="a1"), self.detail())
        self.assertEqual((out.battery_kwh, out.hp, out.seller, out.city), (84, 228, "סמייל", "חיפה"))
        self.assertEqual(classify(out).status, "confirmed")

    def test_detail_flags_sedan_and_hybrid(self):
        six = src_carwiz.parse_detail(L(source="carwiz", id="a1"), self.detail(body="סדאן"))
        self.assertIn("ioniq 6", six.model)
        hybrid = src_carwiz.parse_detail(L(source="carwiz", id="a2"), self.detail(fuelType="היברידי"))
        self.assertIn("hybrid", hybrid.model)

    def test_gone_when_search_page_or_unpublished(self):
        self.assertIsNone(src_carwiz.parse_detail(L(source="carwiz", id="x"), {"resultsPage": {}}))
        page = self.detail()
        page["extendedPost"]["publish"] = False
        self.assertIsNone(src_carwiz.parse_detail(L(source="carwiz", id="x"), page))


# --------------------------------------------------------------------- flow

class FakeSource:
    """Scripted search results; verify() is a dict of id -> Listing/None/Exception."""
    name = "fake"
    listings: list = []
    detail: dict = {}

    def __init__(self, cfg, state_dir):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def search(self):
        return list(self.listings)

    def verify(self, lst):
        out = self.detail.get(lst.id, lst)
        if isinstance(out, Exception):
            raise out
        return out


class TestFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "seen.json"
        self.log = Path(self.tmp.name) / "hits.jsonl"
        self.beat = Path(self.tmp.name) / "beat.json"
        self.cfg = dict(carwatch.DEFAULT_CONFIG, sources=["fake"], telegram=False)
        self.sent = []
        patches = [
            mock.patch.dict(carwatch.SOURCES, {"fake": FakeSource}),
            mock.patch.object(carwatch, "notify_telegram",
                              lambda cfg, t, b: self.sent.append((t, b)) or True),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        FakeSource.detail = {}

    def tearDown(self):
        self.tmp.cleanup()

    def run_poll(self, **kw):
        return carwatch.run_once(self.cfg, self.state, self.log, self.beat, **kw)

    def test_first_run_sends_one_digest_then_alerts_only_new(self):
        FakeSource.listings = [L(id="old", year=2025, trim="Limited", hp=229),
                               L(id="pre", year=2024, trim="Luxury", hp=217)]
        self.run_poll()
        self.assertEqual(len(self.sent), 1)
        self.assertIn("1 listed right now", self.sent[0][0])
        self.assertIn("/item/old", self.sent[0][1])
        self.assertNotIn("/item/pre", self.sent[0][1])

        FakeSource.listings.append(L(id="new", year=2025, trim="Elite", hp=325, price=180000))
        self.run_poll()
        self.assertEqual(len(self.sent), 2)
        title, body = self.sent[1]
        self.assertIn("Ioniq 5 facelift on yad2: ₪180,000", title)
        self.assertIn("325hp", body)
        self.assertIn("/item/new", body)

        self.run_poll()
        self.assertEqual(len(self.sent), 2, "nothing new, nothing sent")
        hits = [json.loads(l) for l in self.log.read_text().splitlines()]
        self.assertEqual([h["id"] for h in hits], ["old", "new"])

    def test_dead_listing_is_never_alerted_but_is_remembered(self):
        FakeSource.listings = [L(id="sold", year=2025, trim="Limited", hp=229)]
        FakeSource.detail = {"sold": None}
        self.run_poll()
        self.assertIn("No facelift", self.sent[0][1])
        self.assertIn("fake:sold", state.load_seen(self.state))

    def test_failed_check_is_retried_not_forgotten(self):
        FakeSource.listings = [L(id="flaky", year=2025, trim="Limited", hp=229)]
        FakeSource.detail = {"flaky": RuntimeError("bot wall")}
        with self.assertRaises(RuntimeError):
            self.run_poll()
        self.assertNotIn("fake:flaky", state.load_seen(self.state))
        FakeSource.detail = {}
        self.run_poll()
        self.assertTrue(any("/item/flaky" in b for _, b in self.sent))

    def test_unverified_is_labelled(self):
        self.state.write_text('{"seen": []}')
        FakeSource.listings = [L(id="maybe", year=2025, trim="Elite")]
        self.run_poll()
        self.assertTrue(self.sent[0][0].startswith("UNVERIFIED"))

    def test_dry_run_changes_nothing(self):
        FakeSource.listings = [L(id="x", year=2025, trim="Limited", hp=229)]
        self.run_poll(dry=True)
        self.assertEqual(self.sent, [])
        self.assertFalse(self.state.exists())

    def test_seed_marks_without_judging(self):
        FakeSource.listings = [L(id="x", year=2025, trim="Limited", hp=229)]
        FakeSource.detail = {"x": RuntimeError("must not be called")}
        carwatch.seed_all(self.cfg, self.state)
        self.assertEqual(state.load_seen(self.state), ["fake:x"])


class TestWatchdogName(unittest.TestCase):
    def test_stale_warning_names_the_unit(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "beat.json"
            state.save_beat(path, {"last_ok": 1})
            sent = []
            with mock.patch.object(state, "notify_telegram",
                                   lambda cfg, t, b: sent.append((t, b)) or True):
                self.assertEqual(state.watchdog(carwatch.DEFAULT_CONFIG, path,
                                                name="carwatch", label="cars"), 1)
            self.assertEqual(sent[0][0], "carwatch has stopped polling")
            self.assertIn("carwatch.timer", sent[0][1])

    def test_a_poll_stamps_the_beat(self):
        with tempfile.TemporaryDirectory() as d:
            beat = Path(d) / "beat.json"
            cfg = dict(carwatch.DEFAULT_CONFIG, sources=[], telegram=False)
            carwatch.run_once(cfg, Path(d) / "seen.json", Path(d) / "hits.jsonl", beat)
            self.assertEqual(state.load_beat(beat)["polls"], 1)


if __name__ == "__main__":
    unittest.main()
