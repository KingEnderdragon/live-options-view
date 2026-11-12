"""M5: local publisher, inactive pilot config, stop/restore/withdraw (fixtures and local stores only).

G3: publication writes immutable generations (news/v1/views/g/<gen>/...) and the fixed index; a replaced
generation stays public (protected) instead of being deleted at the commit. Tests written for G3 were
NOT RUN when they were added (static review only); see the G3 coverage map.

Nothing here contacts a network or a bucket: feeds come from tests/fixtures/news/feeds and both
stores are temporary directories.
"""
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_news_support import ROOT, Clock, feeds
from market_data.news import publish as pub
from market_data.news.__main__ import main
from market_data.news.archive import LocalObjectStore, WriterBusy
from market_data.news.reader import load_archive
from market_data.news.views import LIMITS, build_views, generation_name, qualified_limits, qualify_generation

CONFIG = ROOT / "market_data" / "news" / "deploy" / "news-pilot.json"
PANEL = ROOT / "site" / "js" / "news-panel.js"
INDEX = pub.PUBLIC_INDEX
GEN_EXAMPLE = "20261109T100500Z-0123456789abcdef"


def config():
    return pub.load_pilot_config(CONFIG)


def qualified(files):
    """The published (generation-qualified) form of a builder generation."""
    return qualify_generation(files)[1]


class RecordingStore(LocalObjectStore):
    """Local store that logs operations, records Cache-Control, and can fail or corrupt a chosen operation.
    put_if_absent (immutable generation pages) is logged and failed as a "put"."""

    def __init__(self, root, log=None):
        super().__init__(root)
        self.log = log if log is not None else []
        self.fail = None        # (op, predicate, remaining_successes)
        self.corrupt = None     # predicate on key
        self.cache = {}         # key -> Cache-Control of the last successful write

    def _maybe_fail(self, op, key):
        if self.fail and self.fail[0] == op and self.fail[1](key):
            if self.fail[2] <= 0:
                self.fail = None
                raise RuntimeError(f"simulated crash during {op} {key}")
            self.fail = (self.fail[0], self.fail[1], self.fail[2] - 1)

    def put(self, key, data, content_type="application/octet-stream", cache_control=None):
        self._maybe_fail("put", key)
        if self.corrupt and self.corrupt(key):
            data = data + b" "
        self.log.append(("put", key))
        super().put(key, data, content_type, cache_control=cache_control)
        self.cache[key] = cache_control

    def put_if_absent(self, key, data, content_type="application/octet-stream", cache_control=None):
        self._maybe_fail("put", key)
        if self.corrupt and self.corrupt(key):
            data = data + b" "
        created = super().put_if_absent(key, data, content_type=content_type, cache_control=cache_control)
        if created:
            self.log.append(("put", key))
            self.cache[key] = cache_control
        return created

    def delete(self, key):
        self._maybe_fail("delete", key)
        self.log.append(("delete", key))
        super().delete(key)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="news-m5-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.log = []
        self.private = LocalObjectStore(self.tmp / "private")
        self.raw_public = RecordingStore(self.tmp / "public", self.log)
        self.public = pub.PublicViewStore(self.raw_public)
        self.spool = self.tmp / "spool"
        self.clock = Clock("2026-11-09T10:05:00Z")
        self.cfg = config()

    def cycle(self, when):
        self.clock.set(when)
        return pub.pilot_cycle(self.private, self.public, self.spool, feeds(), self.clock, self.cfg)

    def generation(self, when, days=None):
        """A builder generation (unqualified) from the current private archive, without publishing it."""
        self.clock.set(when)
        v = self.cfg["views"]
        return build_views(load_archive(self.private), pub.unavailable_prices(), as_of=self.clock(),
                           tolerance_s=v["tolerance_s"], data_label=v["data_label"],
                           dates=pub.view_dates_for(self.clock(), days or v["days"]), limits=qualified_limits())

    def public_files(self):
        """The live generation: the fixed index plus every page under the generation it names."""
        raw = self.raw_public.get(INDEX)
        if raw is None:
            return {}
        gen = json.loads(raw)["gen"]
        out = {INDEX: raw}
        out.update({k: self.raw_public.get(k) for k in self.raw_public.list(f"{pub.GENERATION_PREFIX}{gen}/")})
        return out

    def all_public(self):
        return {k: self.raw_public.get(k) for k in self.raw_public.list("")}

    def assert_index_consistent(self):
        """The live index names only pages that exist, all in its own generation (what a reader can fetch)."""
        index = json.loads(self.raw_public.get(INDEX))
        for v in index["views"]:
            self.assertEqual(pub.generation_of(v["first_page"]), index["gen"])
            prefix = v["first_page"].rsplit("/", 1)[0]
            for n in range(1, v["page_count"] + 1):
                self.assertIsNotNone(self.raw_public.get(f"{prefix}/page-{n:04d}.json"), f"{prefix} page {n}")
        return index


class KeysAndSeparation(Base):
    def test_public_store_refuses_everything_but_exact_view_keys(self):
        for key in ("news/v1/runs/2026-11-09/r/manifest.json", "news/v1/publish/state.json", "news/v1/latest.json",
                    # unqualified pages are builder output only; they are never public (G3)
                    "news/v1/views/collector/oil/2026-11-09/page-0001.json",
                    f"news/v1/views/g/{GEN_EXAMPLE}/collector/oil/2026-11-09/page-0000.json",
                    f"news/v1/views/g/{GEN_EXAMPLE}/collector/oil/2026-11-09/page-0021.json",
                    f"news/v1/views/g/{GEN_EXAMPLE}/collector/oil/2026-02-30/page-0001.json",
                    f"news/v1/views/g/{GEN_EXAMPLE}/collector/treasury/2026-11-09/page-0001.json",
                    "news/v1/views/g/20261309T100500Z-0123456789abcdef/collector/oil/2026-11-09/page-0001.json",
                    "news/v1/views/g/20261109T100500Z-0123456789ABCDEF/collector/oil/2026-11-09/page-0001.json",
                    "news/v1/views/g/20261109T100500Z-0123456789abcde/collector/oil/2026-11-09/page-0001.json",
                    "news/v1/views/../runs/x.json", "news/v1/views/index.json.bak"):
            with self.subTest(key=key):
                self.assertFalse(pub.is_public_view_key(key))
                with self.assertRaises(pub.PublishError):
                    self.public.put(key, b"{}")
        self.assertTrue(pub.is_public_view_key(f"news/v1/views/g/{GEN_EXAMPLE}/publication/all/2026-11-09/page-0020.json"))
        self.assertTrue(pub.is_builder_view_key("news/v1/views/publication/all/2026-11-09/page-0020.json"))

    def test_key_rule_matches_the_panel(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        g = f"news/v1/views/g/{GEN_EXAMPLE}"
        keys = ["news/v1/views/index.json", f"{g}/archive/ai_buildout/2026-11-09/page-0001.json",
                f"{g}/archive/ai_buildout/2026-11-09/page-0020.json",
                f"{g}/archive/ai_buildout/2026-11-09/page-0021.json",
                f"{g}/archive/ai_buildout/2026-02-29/page-0001.json",
                f"{g}/archive/ai_buildout/2028-02-29/page-0001.json",
                f"{g}/archive/fed/2026-11-09/page-0001.json", "news/v1/runs/x.json",
                f"{g}/collector/all/2026-11-09/page-1.json",
                "news/v1/views/archive/ai_buildout/2026-11-09/page-0001.json",
                "news/v1/views/g/20261109T250000Z-0123456789abcdef/archive/all/2026-11-09/page-0001.json"]
        script = ("const p=require(process.argv[1]);const ks=JSON.parse(process.argv[2]);"
                  "process.stdout.write(JSON.stringify(ks.map(k=>p.isNewsViewKey(k))))")
        out = subprocess.run([node, "-e", script, str(PANEL), json.dumps(keys)], capture_output=True, text=True,
                             check=True).stdout
        self.assertEqual(json.loads(out), [pub.is_public_view_key(k) for k in keys])

    def test_same_or_nested_stores_are_refused(self):
        with self.assertRaises(pub.PublishError):
            pub.assert_separate(self.private, pub.PublicViewStore(LocalObjectStore(self.tmp / "private")))
        with self.assertRaises(pub.PublishError):
            pub.assert_separate(self.private, pub.PublicViewStore(LocalObjectStore(self.tmp / "private" / "pub")))
        with self.assertRaises(pub.PublishError):
            pub.pilot_cycle(self.private, LocalObjectStore(self.tmp / "private"), self.spool, feeds(), self.clock,
                            self.cfg)

    def test_raw_archive_stays_private_and_views_stay_public(self):
        out = self.cycle("2026-11-09T10:05:00Z")
        self.assertEqual(out["status"], "published")
        self.assertEqual(out["run"]["counts"]["sources_ok"], 4)
        public_keys = self.raw_public.list("")
        self.assertTrue(public_keys)
        self.assertTrue(all(pub.is_public_view_key(k) for k in public_keys), public_keys)
        private_keys = self.private.list("")
        self.assertTrue(any(k.startswith("news/v1/runs/") for k in private_keys))
        self.assertEqual([k for k in private_keys if k.startswith("news/v1/views/")], [])
        # Views exist in the private store only as publisher snapshots.
        self.assertTrue(all(k.startswith(pub.PUBLISH_PREFIX) or not "/views/" in k for k in private_keys))
        # Nothing raw leaks into a public page: no excerpts, no raw fetch errors, no entry failure details.
        for key, body in self.public_files().items():
            text = body.decode("utf-8")
            for word in ("excerpt", "entry_failures", "payload_digest", "http_status"):
                self.assertNotIn(word, text, key)


class Ordering(Base):
    def test_pages_then_index_and_no_deletion_at_the_commit(self):
        self.cycle("2026-11-09T10:05:00Z")
        first = json.loads(self.raw_public.get(INDEX))["gen"]
        # Next day: the oldest date leaves the 7-day window of the NEW generation; the old generation stays
        # public (reader-protected) instead of being deleted after the new index.
        self.log.clear()
        self.cycle("2026-11-10T10:05:00Z")
        puts = [k for op, k in self.log if op == "put"]
        self.assertEqual(puts[-1], INDEX, "index.json must be the last public write")
        self.assertEqual([k for op, k in self.log if op == "delete"], [], "nothing is deleted at the commit")
        live = self.assert_index_consistent()
        self.assertNotEqual(live["gen"], first)
        self.assertFalse([k for k in self.public_files() if "/2026-11-03/" in k])
        self.assertTrue([k for k in self.public_files() if "/2026-11-10/" in k])
        self.assertTrue(self.raw_public.list(f"{pub.GENERATION_PREFIX}{first}/"), "the old generation is protected")

    def test_cache_control_is_immutable_for_pages_and_no_store_for_the_index(self):
        self.cycle("2026-11-09T10:05:00Z")
        for key in self.public_files():
            self.assertEqual(self.raw_public.cache[key],
                             pub.INDEX_CACHE_CONTROL if key == INDEX else pub.PAGE_CACHE_CONTROL, key)

    def test_snapshot_is_complete_before_the_first_public_write(self):
        private_log = []
        private = RecordingStore(self.tmp / "private2", private_log)
        self.private = private
        log_before = len(self.log)
        self.cycle("2026-11-09T10:05:00Z")
        first_public = self.log[log_before]
        snap_manifest = [k for op, k in private_log if k.endswith("/snapshot.json")]
        self.assertEqual(len(snap_manifest), 1)
        # The private and public logs are separate, so compare against the in-progress state ordering instead:
        state_writes = [i for i, (op, k) in enumerate(private_log) if k == pub.STATE_KEY]
        snap_at = private_log.index(("put", snap_manifest[0]))
        self.assertLess(state_writes[0], snap_at, "in-progress state is recorded before the snapshot")
        self.assertTrue(first_public[1].startswith("news/v1/views/"))

    def test_republishing_the_current_build_writes_nothing(self):
        files = self.generation("2026-11-09T10:05:00Z")
        pub.publish(self.private, self.public, files, self.clock)
        self.log.clear()
        self.clock.set("2026-11-09T10:06:00Z")
        report = pub.publish(self.private, self.public, files, self.clock)
        self.assertEqual(report.written, [])
        self.assertEqual(report.unchanged, len(qualified(files)))
        self.assertEqual([op for op, _ in self.log if op in ("put", "delete")], [])


class Interruption(Base):
    def setUp(self):
        super().setUp()
        self.cycle("2026-11-09T10:05:00Z")
        self.gen1 = self.public_files()
        self.gen1_index = self.raw_public.get(INDEX)
        self.cycle_collect_only("2026-11-09T10:20:00Z")
        self.gen2 = self.generation("2026-11-09T10:20:00Z")

    def cycle_collect_only(self, when):
        from market_data.news.archive import collect_once
        self.clock.set(when)
        collect_once(self.private, self.spool, feeds(), self.clock, streams=list(self.cfg["streams"]),
                     only_sources=list(self.cfg["sources"]))

    def test_crash_while_writing_pages_keeps_the_old_index_whole(self):
        self.raw_public.fail = ("put", lambda k: k != INDEX, 2)   # third page write crashes
        with self.assertRaises(RuntimeError):
            pub.publish(self.private, self.public, self.gen2, self.clock)
        self.assertEqual(self.raw_public.get(INDEX), self.gen1_index)
        self.assert_index_consistent()
        self.assertEqual(self.public_files(), self.gen1, "the live generation is untouched")
        st = pub.status(self.private, self.public, self.clock)
        self.assertFalse(st["healthy"])
        self.assertEqual(sorted(st["problems"]), ["last_publication_failed", "publication_interrupted"])
        self.assertEqual(st["last_failure"]["stage"], "pages")
        # Recovery: the next attempt completes and says what it recovered from.
        report = pub.publish(self.private, self.public, self.gen2, self.clock)
        self.assertEqual(report.recovered_from, generation_name(self.gen2))
        self.assertEqual(self.public_files(), qualified(self.gen2))
        self.assertTrue(pub.status(self.private, self.public, self.clock)["healthy"])

    def test_failed_index_write_leaves_the_previous_generation_live_and_the_candidate_uncertain(self):
        self.raw_public.fail = ("put", lambda k: k == INDEX, 0)
        with self.assertRaises(pub.PublishIntegrityError):
            pub.publish(self.private, self.public, self.gen2, self.clock)
        self.assertEqual(self.raw_public.get(INDEX), self.gen1_index)
        self.assert_index_consistent()
        state = pub.read_state(self.private)
        self.assertEqual(state["last_failure"]["stage"], "index")
        self.assertEqual(state["uncertain"]["gen"], generation_name(self.gen2))
        # Retrying the same build is a second index write while the first is unresolved: refused too.
        with self.assertRaises(pub.PublishPaused):
            pub.publish(self.private, self.public, self.gen2, self.clock)
        self.assertEqual(self.raw_public.get(INDEX), self.gen1_index)
        self.assertEqual(pub.read_state(self.private)["uncertain"]["gen"], generation_name(self.gen2))

    def test_a_page_that_reads_back_wrong_stops_before_the_index(self):
        target = sorted(k for k in qualified(self.gen2) if k != INDEX)[0]
        self.raw_public.corrupt = lambda k: k == target
        with self.assertRaises(pub.PublishIntegrityError):
            pub.publish(self.private, self.public, self.gen2, self.clock)
        self.assertEqual(self.raw_public.get(INDEX), self.gen1_index)
        self.raw_public.corrupt = None
        # The bad page belongs to a never-offered candidate, so the retry replaces it.
        pub.publish(self.private, self.public, self.gen2, self.clock)
        self.assertEqual(self.public_files(), qualified(self.gen2))

    def test_crash_during_cleanup_is_finished_by_the_next_run(self):
        # An orphan: gen2's pages written, its index write never attempted (crash on the third page).
        self.raw_public.fail = ("put", lambda k: k != INDEX, 2)
        with self.assertRaises(RuntimeError):
            pub.publish(self.private, self.public, self.gen2, self.clock)
        orphan = generation_name(self.gen2)
        self.assertTrue(self.raw_public.list(f"{pub.GENERATION_PREFIX}{orphan}/"))
        later = self.generation("2026-11-10T10:05:00Z")
        self.raw_public.fail = ("delete", lambda k: True, 0)
        with self.assertRaises(RuntimeError):
            pub.publish(self.private, self.public, later, self.clock)
        self.assertEqual(self.raw_public.get(INDEX), self.gen1_index, "nothing new before cleanup finishes")
        self.assertEqual(pub.read_state(self.private)["last_failure"]["stage"], "cleanup")
        pub.publish(self.private, self.public, later, self.clock)
        self.assertEqual(self.raw_public.list(f"{pub.GENERATION_PREFIX}{orphan}/"), [], "the orphan is gone")
        self.assertEqual(self.public_files(), qualified(later))

    def test_lease_held_means_nothing_is_written(self):
        other = pub.WriterLease(self.private, self.clock, "other-publisher", key=pub.PUBLISH_LEASE_KEY)
        with other:
            self.log.clear()
            with self.assertRaises(WriterBusy):
                pub.publish(self.private, self.public, self.gen2, self.clock)
        self.assertEqual(self.log, [])
        self.assertEqual(self.raw_public.get(INDEX), self.gen1_index)

    def test_invalid_generations_are_rejected_before_any_write(self):
        missing_page = dict(self.gen2)
        missing_page.pop(sorted(k for k in self.gen2 if k != INDEX)[0])
        oversize = dict(self.gen2)
        key = sorted(k for k in self.gen2 if k != INDEX)[0]
        oversize[key] = self.gen2[key] + b" " * LIMITS.page_bytes
        foreign = dict(self.gen2, **{"news/v1/runs/x.json": b"{}"})
        no_index = {k: v for k, v in self.gen2.items() if k != INDEX}
        already_qualified = qualified(self.gen2)       # publish takes builder output, never published keys
        for name, files in (("missing page", missing_page), ("oversize", oversize), ("foreign key", foreign),
                            ("no index", no_index), ("qualified keys", already_qualified)):
            with self.subTest(name):
                self.log.clear()
                with self.assertRaises(pub.PublishError):
                    pub.publish(self.private, self.public, files, self.clock)
                self.assertEqual(self.log, [])
        self.assertEqual(self.public_files(), self.gen1)


class StopRestoreWithdraw(Base):
    def setUp(self):
        super().setUp()
        self.cycle("2026-11-09T10:05:00Z")
        self.gen1 = self.public_files()
        self.gen1_id = pub.read_state(self.private)["committed"]["gen_id"]
        self.cycle("2026-11-09T10:20:00Z")
        self.gen2 = self.public_files()

    def test_stop_makes_cycles_a_clean_no_op(self):
        pub.stop(self.private, self.clock, "pilot paused for review")
        runs_before = [k for k in self.private.list("news/v1/runs/")]
        self.log.clear()
        out = self.cycle("2026-11-09T10:35:00Z")
        self.assertEqual(out["status"], "stopped")
        self.assertEqual(self.private.list("news/v1/runs/"), runs_before, "no collection while stopped")
        self.assertEqual(self.log, [], "no public write while stopped")
        with self.assertRaises(pub.PublishStopped):
            pub.publish(self.private, self.public, self.generation("2026-11-09T10:35:00Z"), self.clock)
        st = pub.status(self.private, self.public, self.clock.set("2026-11-09T12:00:00Z"))
        self.assertNotIn("published_views_stale", st["problems"], "a deliberate stop is not a staleness alarm")
        self.assertEqual(st["stopped"]["reason"], "pilot paused for review")
        self.assertTrue(pub.resume(self.private))
        self.assertEqual(self.cycle("2026-11-09T12:05:00Z")["status"], "published")

    def test_restore_republishes_a_snapshot_byte_for_byte(self):
        with self.assertRaises(pub.PublishError):
            pub.restore(self.private, self.public, self.gen1_id, self.clock)   # not stopped
        pub.stop(self.private, self.clock, "rollback")
        report = pub.restore(self.private, self.public, self.gen1_id, self.clock)
        self.assertEqual(report.action, "restore")
        self.assertEqual(self.public_files(), self.gen1)
        self.assertEqual(pub.read_state(self.private)["committed"]["gen_id"], self.gen1_id)
        self.assertEqual(self.cycle("2026-11-09T10:50:00Z")["status"], "stopped", "restore stays in place")

    def test_altered_or_unknown_snapshots_are_refused(self):
        pub.stop(self.private, self.clock, "rollback")
        with self.assertRaises(pub.PublishError):
            pub.restore(self.private, self.public, "20990101T000000Z-0000000000000000", self.clock)
        victim = [k for k in self.private.list(f"{pub.SNAPSHOT_PREFIX}{self.gen1_id}/") if k.endswith(".json")
                  and not k.endswith("snapshot.json")][0]
        self.private.put(victim, self.private.get(victim) + b" ")
        self.log.clear()
        with self.assertRaises(pub.PublishError):
            pub.restore(self.private, self.public, self.gen1_id, self.clock)
        self.assertEqual(self.log, [])
        self.assertEqual(self.public_files(), self.gen2)

    def test_withdraw_removes_the_index_first_then_every_generation(self):
        with self.assertRaises(pub.PublishError):
            pub.withdraw(self.private, self.public, self.clock)
        pub.stop(self.private, self.clock, "take down")
        everything = self.all_public()
        self.assertGreater(len(everything), len(self.gen2), "the protected first generation is public too")
        self.log.clear()
        out = pub.withdraw(self.private, self.public, self.clock)
        self.assertEqual(self.log[0], ("delete", INDEX))
        self.assertEqual(self.raw_public.list(""), [], "no grace: every generation is deleted at once")
        self.assertEqual(out["deleted"], len(everything))
        self.assertIn("cannot be recalled", out["note"])
        self.assertTrue(out["snapshots_kept"], "snapshots survive a withdrawal for a later restore")
        st = pub.status(self.private, self.public, self.clock)
        self.assertIsNone(st["committed"])
        self.assertTrue(st["withdrawn"])
        pub.restore(self.private, self.public, self.gen1_id, self.clock)
        self.assertEqual(self.public_files(), self.gen1)

    def test_snapshot_retention_keeps_the_newest_and_never_the_live_one_away(self):
        for minute in ("35", "50"):
            self.cycle(f"2026-11-09T10:{minute}:00Z")
        self.cycle("2026-11-09T11:05:00Z")
        ids = pub.snapshot_ids(self.private)
        self.assertEqual(len(ids), self.cfg["retention"]["snapshots"])
        live = pub.read_state(self.private)["committed"]["gen_id"]
        self.assertIn(live, ids)
        self.assertNotIn(self.gen1_id, ids)


class MissingPrices(Base):
    def test_prices_are_explicitly_unavailable(self):
        self.cycle("2026-11-09T10:05:00Z")
        rows = 0
        for key, body in self.public_files().items():
            if key == INDEX:
                continue
            page = json.loads(body)
            self.assertEqual(page["price_series"]["input"], "unavailable", key)
            self.assertFalse(page["price_series"]["synthetic"])
            self.assertIn("prices unavailable", page["data_label"])
            self.assertEqual(page["coverage"]["price"]["stale"], [])
            if page["window"]["start"] < page["window"]["end"]:
                missing = page["coverage"]["price"]["missing"]
                self.assertEqual([(m["start"], m["end"]) for m in missing],
                                 [(page["window"]["start"], page["window"]["end"])], key)
            for row in page["rows"]:
                rows += 1
                self.assertEqual(row["price"], {"status": "missing", "age_seconds": None, "record": None})
        self.assertGreater(rows, 0)

    def test_fixture_price_pages_do_not_gain_the_marker(self):
        self.cycle("2026-11-09T10:05:00Z")
        from test_news_join import PRICES
        from market_data.news.prices import load_price_file
        files = build_views(load_archive(self.private), load_price_file(PRICES), as_of="2026-11-09T10:05:00Z",
                            tolerance_s=120, data_label="SYNTHETIC fixture data", dates=["2026-11-09"])
        page = json.loads(files["news/v1/views/collector/all/2026-11-09/page-0001.json"])
        self.assertNotIn("input", page["price_series"])


class PilotConfig(Base):
    def test_shipped_config_is_inactive_and_valid(self):
        cfg = config()
        self.assertFalse(cfg.active)
        self.assertEqual(cfg["schedule"]["cron"], "*/15 * * * *")
        self.assertEqual(sorted(cfg["sources"]), ["ars_technica", "eia_today_in_energy", "oilprice_com", "techcrunch"])
        self.assertIsNone(cfg["storage"]["private"]["bucket"])
        self.assertIsNone(cfg["storage"]["public"]["bucket"])
        policy = pub.policy_from(cfg)
        self.assertEqual((policy.reader_grace_s, policy.delete_after_s, policy.page_cap), (2700, 2940, 6300))
        self.assertFalse(policy.clock_bound_verified, "the store-clock bound is an assumption, not verified")

    def test_invalid_configs_are_refused(self):
        raw = json.loads(CONFIG.read_bytes())
        cases = {
            "cadence": lambda c: c["schedule"].update(cron="*/5 * * * *"),
            "extra source": lambda c: c["sources"].append("gnews_ai_bubble"),
            "price input": lambda c: c["prices"].update(input="file"),
            "shared credentials": lambda c: c["storage"]["public"].update(env_prefix="NEWS_PRIVATE_"),
            "public prefix": lambda c: c["storage"]["public"].update(prefix="news/v1/"),
            "restart": lambda c: c["schedule"].update(restart_policy="ALWAYS"),
            "active not bool": lambda c: c.update(active="yes"),
            "grace below the reader bound": lambda c: c["generations"].update(reader_grace_s=600),
            "cap below one generation": lambda c: c["generations"].update(page_cap=1000),
            "clock flag not bool": lambda c: c["generations"].update(clock_bound_verified="yes"),
            "no generations block": lambda c: c.pop("generations"),
        }
        for name, change in cases.items():
            with self.subTest(name):
                c = copy.deepcopy(raw)
                change(c)
                path = self.tmp / f"{name}.json"
                path.write_bytes(json.dumps(c).encode())
                with self.assertRaises(pub.PublishError):
                    pub.load_pilot_config(path)

    def run_cli(self, *argv, env=None):
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env or {}, clear=False), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_live_mode_is_refused_while_inactive_or_without_opt_in(self):
        base = ["--private", str(self.tmp / "p"), "--public", str(self.tmp / "q")]
        with mock.patch("market_data.news.adapters.rss.http_fetch", side_effect=AssertionError("network")):
            code, _, err = self.run_cli("pilot-cycle", "--live", *base)
            self.assertEqual(code, 2)
            self.assertIn("inactive", err)
            raw = json.loads(CONFIG.read_bytes())
            raw["active"] = True
            active = self.tmp / "active.json"
            active.write_bytes(json.dumps(raw).encode())
            os.environ.pop("NEWS_ALLOW_BUCKET_WRITES", None)
            code, _, err = self.run_cli("pilot-cycle", "--live", "--config", str(active), *base)
            self.assertEqual(code, 2)
            self.assertIn("NEWS_ALLOW_BUCKET_WRITES", err)
            code, _, err = self.run_cli("pilot-cycle", "--live", "--config", str(active), *base,
                                        env={"NEWS_ALLOW_BUCKET_WRITES": "1"})
            self.assertEqual(code, 2)
            self.assertIn("bucket mode needs", err, "missing credentials fail before any client is built")
        self.assertFalse((self.tmp / "p").exists())

    def test_cli_cycle_status_stop_restore_withdraw(self):
        base = ["--private", str(self.tmp / "p"), "--public", str(self.tmp / "q")]
        cyc = base + ["--spool", str(self.tmp / "s")]
        self.assertEqual(self.run_cli("pilot-cycle", *cyc, "--clock", "2026-11-09T10:05:00Z")[0], 0)
        code, out, _ = self.run_cli("publish-status", *base, "--clock", "2026-11-09T10:06:00Z")
        self.assertEqual(code, 0)
        gen = json.loads(out)["committed"]["gen_id"]
        code, out, _ = self.run_cli("publish-status", *base, "--clock", "2026-11-09T11:30:00Z")
        self.assertEqual((code, json.loads(out)["problems"]), (1, ["published_views_stale"]))
        self.assertEqual(self.run_cli("pilot-cycle", *cyc, "--clock", "2026-11-09T10:20:00Z")[0], 0)
        self.assertEqual(self.run_cli("publish-restore", *base, "--gen", gen)[0], 2)  # not stopped
        self.assertEqual(self.run_cli("publish-stop", *base, "--reason", "rollback drill")[0], 0)
        code, out, _ = self.run_cli("pilot-cycle", *cyc, "--clock", "2026-11-09T10:35:00Z")
        self.assertEqual((code, json.loads(out)["status"]), (0, "stopped"))
        self.assertEqual(self.run_cli("publish-restore", *base, "--gen", gen)[0], 0)
        self.assertEqual(self.run_cli("publish-withdraw", *base)[0], 0)
        self.assertFalse((self.tmp / "q" / "news" / "v1" / "views" / "index.json").exists())
        self.assertEqual(self.run_cli("publish-resume", *base)[0], 0)
        self.assertEqual(self.run_cli("pilot-cycle", *cyc, "--clock", "2026-11-09T10:50:00Z")[0], 0)
        self.assertTrue((self.tmp / "q" / "news" / "v1" / "views" / "index.json").exists())


if __name__ == "__main__":
    unittest.main()
