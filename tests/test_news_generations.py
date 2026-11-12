"""G3: generation-qualified publication lifecycle (the accepted G2 v2 design with Chej's corrections).

NOT RUN: these tests were written for static review (Chej's G3 brief: no test execution). Local only:
fixture feeds and temporary directory stores; the store clock is a controllable fake (a real bucket's
LastModified would be the store clock), so elapsed-time rules can be checked exactly.

Covered: qualification (logical vs final digests, same-generation links), the published key grammar,
the lifecycle ledger and latest retirement epoch, cleanup before admission (orphans, expiry), the clock
rules (both clocks, agreement, backward clock, unverified bound refuses expiry visibly), uncertain index
writes (old index never proves an orphan; settlement only by a full live match, never by elapsed time),
blocked runs (unmatched live index, unreadable ledger), unknown public generations, the page cap
(publication and restore count missing pages; never evict), and immediate withdrawal.
"""
import json
import unittest

from test_news_publish import INDEX, Base, RecordingStore, qualified
from test_news_support import Clock
from market_data.news import publish as pub
from market_data.news.archive import LocalObjectStore
from market_data.news.views import (BuildError, files_digest, generation_name, pages_digest,
                                    qualify_generation)

VERIFIED = pub.GenerationPolicy(clock_bound_verified=True)


class ClockedStore(LocalObjectStore):
    """A private store whose modification times come from a fake store clock (like a bucket's LastModified)."""

    def __init__(self, root, store_clock):
        super().__init__(root)
        self.store_clock = store_clock

    def last_modified(self, key):
        return self.store_clock() if self.get(key) is not None else None


class AckStore(RecordingStore):
    """Public store that can lose the acknowledgement of an index write: the write lands, the call fails."""

    lose_index_ack = False

    def put(self, key, data, content_type="application/octet-stream", cache_control=None):
        super().put(key, data, content_type, cache_control=cache_control)
        if key == INDEX and self.lose_index_ack:
            self.lose_index_ack = False
            raise RuntimeError("timeout after the write landed")


class Gen(Base):
    def setUp(self):
        super().setUp()
        self.store_clock = Clock("2026-11-09T10:05:00Z")
        self.private = ClockedStore(self.tmp / "private-g", self.store_clock)
        self.raw_public = AckStore(self.tmp / "public-g", self.log)
        self.public = pub.PublicViewStore(self.raw_public)
        self.cycle("2026-11-09T10:05:00Z")
        self.A = pub.read_state(self.private)["committed"]["gen_id"]

    def build(self, when, store=None):
        files = self.generation(when)          # sets the producer clock to `when`
        self.store_clock.set(store or when)
        return files

    def at(self, when, store=None):
        self.clock.set(when)
        self.store_clock.set(store or when)

    def publish(self, files, policy=pub.DEFAULT_POLICY):
        return pub.publish(self.private, self.public, files, self.clock, policy=policy)

    def ledger(self):
        records, problems = pub.read_ledger(self.private)
        self.assertEqual(problems, [])
        return records

    def kinds(self, gen):
        return [e["kind"] for e in self.ledger()[gen]["events"]]

    def pages(self, gen):
        return self.raw_public.list(f"{pub.GENERATION_PREFIX}{gen}/")

    def live(self):
        return json.loads(self.raw_public.get(INDEX))["gen"]


class Qualification(Gen):
    def test_logical_identity_is_fixed_before_keys_and_final_integrity_is_separate(self):
        files = self.build("2026-11-09T10:20:00Z")
        gen, out = qualify_generation(files)
        self.assertEqual(qualify_generation(files), (gen, out), "deterministic")
        self.assertEqual(gen, generation_name(files))
        self.assertEqual(gen, "20261109T102000Z-" + files_digest(files)[:16], "hash of the UNQUALIFIED build")
        index = json.loads(out[INDEX])
        self.assertEqual(index["gen"], gen)
        self.assertEqual(index["pages_sha256"], pages_digest(out), "final integrity of the qualified pages")
        self.assertNotEqual(files_digest(files)[:16], files_digest(out)[:16])
        for key, body in out.items():
            if key == INDEX:
                continue
            self.assertEqual(pub.generation_of(key), gen)
            page = json.loads(body)
            self.assertEqual(page["gen"], gen)
            for link in (page.get("prev_page"), page.get("next_page")):
                self.assertIn(link if link is None else pub.generation_of(link), (None, gen))
        for v in index["views"]:
            self.assertEqual(pub.generation_of(v["first_page"]), gen)
        self.assertEqual(pub.validate_qualified(gen, out)["gen"], gen)

    def test_a_link_outside_the_build_cannot_be_qualified(self):
        files = dict(self.build("2026-11-09T10:20:00Z"))
        key = sorted(k for k in files if k != INDEX)[0]
        page = json.loads(files[key])
        page["next_page"] = "news/v1/views/collector/oil/2026-11-08/page-0007.json"
        files[key] = json.dumps(page).encode()
        with self.assertRaises(BuildError):
            qualify_generation(files)

    def test_validate_qualified_refuses_cross_generation_and_digest_mismatches(self):
        files = self.build("2026-11-09T10:20:00Z")
        gen, out = qualify_generation(files)
        other = "20990101T000000Z-0123456789abcdef"
        key = sorted(k for k in out if k != INDEX)[0]
        index = json.loads(out[INDEX])
        page = json.loads(out[key])
        cases = {
            "page under another generation": {**{k: v for k, v in out.items() if k != key},
                                              key.replace(gen, other): out[key]},
            "page.gen differs": dict(out, **{key: json.dumps(dict(page, gen=other)).encode()}),
            "pages_sha256 differs": dict(out, **{INDEX: json.dumps(dict(index, pages_sha256="0" * 64)).encode()}),
            "index.gen differs": dict(out, **{INDEX: json.dumps(dict(index, gen=other)).encode()}),
        }
        for name, bad in cases.items():
            with self.subTest(name):
                with self.assertRaises(pub.PublishError):
                    pub.validate_qualified(gen, bad)


class Lifecycle(Gen):
    def test_a_replaced_generation_is_ceased_and_stays_public(self):
        B = generation_name(self.build("2026-11-09T10:20:00Z"))
        self.publish(self.generation("2026-11-09T10:20:00Z"))
        self.assertEqual(self.live(), B)
        self.assertEqual(self.kinds(self.A)[-1], "ceased")
        self.assertIn("published", self.kinds(B))
        self.assertTrue(self.pages(self.A), "reader-protected")
        st = pub.status(self.private, self.public, self.clock)
        self.assertEqual((st["generations"][self.A]["class"], st["generations"][B]["class"]), ("protected", "current"))
        self.assertIn("earliest_deletion_producer_clock", st["generations"][self.A])

    def test_expiry_is_refused_visibly_while_the_clock_bound_is_unverified(self):
        self.publish(self.build("2026-11-09T10:20:00Z"))
        self.publish(self.build("2026-11-09T12:20:00Z"))          # two hours later, default policy
        self.assertTrue(self.pages(self.A), "never deleted on an unverified clock bound")
        self.assertNotIn("pages_deleted", self.kinds(self.A))
        st = pub.status(self.private, self.public, self.clock)
        self.assertIn("expiry_blocked_clock_bound_unverified", st["problems"])
        self.assertFalse(st["policy"]["clock_bound_verified"])

    def test_expiry_needs_both_clocks_past_grace_epsilon_and_skew(self):
        self.publish(self.build("2026-11-09T10:20:00Z"), VERIFIED)      # A ceases at 10:20 on both clocks
        self.assertEqual(VERIFIED.delete_after_s, 49 * 60)
        self.publish(self.build("2026-11-09T11:08:00Z"), VERIFIED)      # 48 min: protected
        self.assertTrue(self.pages(self.A))
        self.publish(self.build("2026-11-09T11:10:00Z"), VERIFIED)      # 50 min: expired, deleted before admission
        self.assertEqual(self.pages(self.A), [])
        deleted = [e for e in self.ledger()[self.A]["events"] if e["kind"] == "pages_deleted"]
        self.assertEqual(deleted[-1]["reason"], "expired")

    def test_a_forward_producer_clock_jump_alone_deletes_nothing(self):
        self.publish(self.build("2026-11-09T10:20:00Z"), VERIFIED)
        files = self.build("2026-11-10T10:20:00Z", store="2026-11-09T10:35:00Z")   # producer +1 day, store +15 min
        self.publish(files, VERIFIED)
        self.assertTrue(self.pages(self.A), "store elapsed is 15 min, and the clocks disagree")
        self.assertNotIn("pages_deleted", self.kinds(self.A))

    def test_a_backward_producer_clock_refuses_deletion(self):
        self.publish(self.build("2026-11-09T10:20:00Z"), VERIFIED)
        files = self.build("2026-11-09T10:10:00Z", store="2026-11-09T12:00:00Z")   # producer went backwards
        self.publish(files, VERIFIED)
        self.assertTrue(self.pages(self.A))
        self.assertIn("producer clock is earlier than its last recorded reading",
                      pub.read_state(self.private)["clock"]["refused"])

    def test_protection_follows_the_latest_epoch_after_a_restore(self):
        self.publish(self.build("2026-11-09T10:20:00Z"), VERIFIED)      # A ceased 10:20
        pub.stop(self.private, self.clock, "rollback")
        self.at("2026-11-09T10:30:00Z")
        pub.restore(self.private, self.public, self.A, self.clock, policy=VERIFIED)   # A current again
        self.assertEqual(self.kinds(self.A)[-1], "restored")
        pub.resume(self.private)
        self.publish(self.build("2026-11-09T11:00:00Z"), VERIFIED)      # A ceased again at 11:00
        self.publish(self.build("2026-11-09T11:15:00Z"), VERIFIED)      # 55 min after the FIRST cease
        self.assertTrue(self.pages(self.A), "the old cease no longer counts")
        self.publish(self.build("2026-11-09T11:50:00Z"), VERIFIED)      # 50 min after the latest cease
        self.assertEqual(self.pages(self.A), [])


class Orphans(Gen):
    def test_a_never_offered_candidate_is_deleted_by_the_next_run(self):
        B_files = self.build("2026-11-09T10:20:00Z")
        B = generation_name(B_files)
        self.raw_public.fail = ("put", lambda k: k != INDEX, 2)
        with self.assertRaises(RuntimeError):
            self.publish(B_files)
        self.assertTrue(self.pages(B))
        self.assertNotIn("index_put_attempted", self.kinds(B))
        self.publish(self.build("2026-11-09T10:35:00Z"))                # default policy: orphans need no clock
        self.assertEqual(self.pages(B), [])
        self.assertEqual([e for e in self.ledger()[B]["events"] if e["kind"] == "pages_deleted"][-1]["reason"], "orphan")


class Uncertain(Gen):
    def test_an_old_index_never_proves_an_uncertain_candidate_is_an_orphan(self):
        B_files = self.build("2026-11-09T10:20:00Z")
        B = generation_name(B_files)
        self.raw_public.fail = ("put", lambda k: k == INDEX, 0)
        with self.assertRaises(pub.PublishIntegrityError):
            self.publish(B_files)
        self.assertEqual(self.live(), self.A, "the old index is still live")
        self.log.clear()
        with self.assertRaises(pub.PublishPaused):
            self.publish(self.build("2026-11-09T10:35:00Z"), VERIFIED)
        self.assertTrue(self.pages(B), "retained")
        self.assertEqual([op for op, k in self.log if k == INDEX], [], "no conflicting index write")
        st = pub.status(self.private, self.public, self.clock)
        self.assertIn("uncertain_index_write", st["problems"])
        self.assertIn("publication_paused", st["problems"])
        self.assertEqual(st["generations"][B]["class"], "uncertain")

    def test_settled_by_a_full_live_match_when_the_lost_write_landed(self):
        B_files = self.build("2026-11-09T10:20:00Z")
        B = generation_name(B_files)
        self.raw_public.lose_index_ack = True
        with self.assertRaises(pub.PublishIntegrityError):
            self.publish(B_files)
        self.assertEqual(self.live(), B, "the write landed although the call failed")
        C_files = self.build("2026-11-09T10:35:00Z")
        self.publish(C_files)
        self.assertIn("published", self.kinds(B))
        self.assertEqual(self.kinds(self.A)[-1], "ceased")
        self.assertEqual(self.kinds(B)[-1], "ceased")
        self.assertIsNone(pub.read_state(self.private)["uncertain"])
        self.assertEqual(self.public_files(), qualified(C_files))

    def test_elapsed_time_never_releases_an_uncertain_candidate(self):
        B_files = self.build("2026-11-09T10:20:00Z")
        B = generation_name(B_files)
        self.raw_public.fail = ("put", lambda k: k == INDEX, 0)
        with self.assertRaises(pub.PublishIntegrityError):
            self.publish(B_files)
        self.assertFalse(hasattr(pub, "fence"), "no timer-only or operator release exists")
        with self.assertRaises(pub.PublishPaused):
            self.publish(self.build("2026-11-09T12:20:00Z"), VERIFIED)   # two hours later on both clocks
        self.assertTrue(self.pages(B), "still retained")
        self.assertEqual(self.live(), self.A)
        pub.stop(self.private, self.clock, "settle B")
        pub.restore(self.private, self.public, B, self.clock, policy=VERIFIED)   # the same generation, live in full
        self.assertEqual(self.live(), B)
        self.assertEqual(self.kinds(B)[-1], "restored")
        self.assertIsNone(pub.read_state(self.private)["uncertain"])


class Blocked(Gen):
    def test_an_unmatched_live_index_blocks_everything(self):
        self.raw_public.put(INDEX, b'{"schema_version": "news-view-index/v1", "views": []}')
        self.log.clear()
        with self.assertRaises(pub.PublishBlocked):
            self.publish(self.build("2026-11-09T10:20:00Z"), VERIFIED)
        self.assertEqual([e for e in self.log if e[0] in ("put", "delete")], [])
        st = pub.status(self.private, self.public, self.clock)
        self.assertIn("publication_blocked", st["problems"])
        self.assertIn("live_index_unmatched", st["problems"])

    def test_an_unreadable_ledger_blocks_everything(self):
        self.private.put(f"{pub.LEDGER_PREFIX}{self.A}.json", b"{not json")
        self.log.clear()
        with self.assertRaises(pub.PublishBlocked):
            self.publish(self.build("2026-11-09T10:20:00Z"), VERIFIED)
        self.assertEqual([e for e in self.log if e[0] in ("put", "delete")], [])

    def test_unknown_public_generations_are_retained_and_reported(self):
        stray = "news/v1/views/g/20261109T090000Z-aaaaaaaaaaaaaaaa/collector/oil/2026-11-09/page-0001.json"
        self.raw_public.put(stray, b"{}")
        self.publish(self.build("2026-11-09T12:00:00Z"), VERIFIED)
        self.assertIsNotNone(self.raw_public.get(stray))
        st = pub.status(self.private, self.public, self.clock)
        self.assertIn("unknown_public_generations", st["problems"])


class Cap(Gen):
    def test_the_cap_pauses_after_cleanup_and_never_evicts(self):
        orphan_files = self.build("2026-11-09T10:20:00Z")
        orphan = generation_name(orphan_files)
        self.raw_public.fail = ("put", lambda k: k != INDEX, 3)
        with self.assertRaises(RuntimeError):
            self.publish(orphan_files)
        C_files = self.build("2026-11-09T10:35:00Z")
        need = len(self.pages(self.A)) + len(qualified(C_files)) - 1
        tight = pub.GenerationPolicy(page_cap=need - 1)
        with self.assertRaises(pub.PublishPaused):
            self.publish(C_files, tight)
        self.assertEqual(self.pages(orphan), [], "cleanup ran before admission")
        self.assertTrue(self.pages(self.A), "the current generation is never evicted")
        self.assertIn("page cap", pub.read_state(self.private)["paused"]["why"])
        self.publish(C_files, pub.GenerationPolicy(page_cap=need))
        self.assertEqual(self.public_files(), qualified(C_files))
        self.assertIsNone(pub.read_state(self.private)["paused"])

    def test_restore_counts_its_missing_pages_against_the_cap(self):
        self.publish(self.build("2026-11-09T10:20:00Z"), VERIFIED)
        self.publish(self.build("2026-11-09T11:10:00Z"), VERIFIED)       # A expired and deleted
        self.assertEqual(self.pages(self.A), [])
        pub.stop(self.private, self.clock, "rollback")
        self.at("2026-11-09T11:12:00Z")
        present = sum(len(v) for v in self.public.pages_by_gen().values())
        a_pages = len([k for k in pub.load_snapshot(self.private, self.A) if k != INDEX])
        with self.assertRaises(pub.PublishPaused):
            pub.restore(self.private, self.public, self.A, self.clock,
                        policy=pub.GenerationPolicy(page_cap=present + a_pages - 1, clock_bound_verified=True))
        pub.restore(self.private, self.public, self.A, self.clock, policy=VERIFIED)
        self.assertEqual(self.live(), self.A)
        self.assertEqual(self.kinds(self.A)[-1], "restored")


class Withdraw(Gen):
    def test_withdrawal_deletes_every_generation_at_once(self):
        B_files = self.build("2026-11-09T10:20:00Z")
        B = generation_name(B_files)
        self.publish(B_files)
        self.assertTrue(self.pages(self.A) and self.pages(B))
        pub.stop(self.private, self.clock, "revoke")
        out = pub.withdraw(self.private, self.public, self.clock)
        self.assertEqual(self.raw_public.list(""), [], "protected pages too: no grace")
        self.assertIn("cannot be recalled", out["note"])
        self.assertEqual(self.kinds(B)[-2:], ["ceased", "pages_deleted"])
        self.assertEqual(self.kinds(self.A)[-1], "pages_deleted")


if __name__ == "__main__":
    unittest.main()
