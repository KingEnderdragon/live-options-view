"""Publish sanitized news views (M5, local preparation; G3 generations): private archive -> public views.

Two stores, never the same one:

  private   the raw archive (news/v1/runs/, indexes, latest.json, writer lease)
            plus the publisher's own state under news/v1/publish/:
              state.json                  committed generation, in-progress target, uncertain/blocked/paused,
                                          last failure, clock high-water marks
              heartbeat.json              rewritten every run; its store modification time is the store clock
              generations/<gen>.json      the lifecycle ledger: digests, page set and events
              stop.json                   operator stop marker (cycles and publication no-op)
              lease.json                  single-publisher lease
              snapshots/<gen>/...         exact bytes of recent published generations (restore)
              receipts/<time>-<gen>.json  one per publication / restore / withdrawal
  public    only the exact view keys the panel and proxy accept:
              news/v1/views/index.json                                            the commit point
              news/v1/views/g/<gen>/<mode>/<stream>/<YYYY-MM-DD>/page-NNNN.json  immutable

A generation (G3, the accepted G2 v2 design) is the qualified form of one build (views.qualify_generation):
gen = <build_as_of>-<16 hex of the unqualified build's digest>, every page under g/<gen>/ with links that
stay in that generation, and an index naming gen and pages_sha256. Each run:

  1. reads the clock (a heartbeat: producer time and the store's own time) and the ledger;
  2. reconciles: the live index must match one recorded generation in full (index hash, gen,
     pages_sha256, every page hash); that one is current, every other one recorded as current is
     ceased now. Anything unmatched or unreadable blocks the run (nothing written or deleted);
  3. cleans up: deletes orphan pages (written, never offered) and expired pages (ceased, both clocks
     past reader grace + epsilon + skew, clocks agreeing, and only once the clock bound is verified);
     never current, protected, uncertain or unknown pages; needs no new commit;
  4. admits: an unsettled index write anywhere pauses publication; so does the page cap
     (present + missing pages > cap). Nothing protected is evicted;
  5. writes the generation's pages create-if-absent (immutable; different bytes at an existing key stop
     the run), then the index, then reconciles again. An index write that fails or does not read back
     leaves the candidate uncertain until a later run sees it live in full. Neither elapsed time nor a
     retry of the same generation releases it, and an older lifecycle record cannot settle it.

Withdrawal deletes the index and every generation's pages at once; cached copies cannot be recalled.
Nothing here contacts a network or a bucket unless a caller passes such a store.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config
from .archive import (LEASE_KEY, ArchiveError, LocalObjectStore, R2ObjectStore, WriterLease, check_key,
                      collect_once, encode_json)
from .normalize import iso, parse_iso, sha256_hex
from .prices import PriceFile
from .reader import MODES, load_archive
from .views import (GEN_PATTERN, INDEX_REASONS, LIMITS, ROOT, STREAM_KEYS, build_views, files_digest,
                    generation_name, generation_page_key, is_gen, page_key, pages_digest, qualified_limits,
                    qualify_generation)

PUBLIC_PREFIX = f"{ROOT}/"
PUBLIC_INDEX = f"{ROOT}/index.json"
GENERATION_PREFIX = f"{ROOT}/g/"
PUBLISH_PREFIX = f"{config.NAMESPACE}/publish/"
STATE_KEY = PUBLISH_PREFIX + "state.json"
STOP_KEY = PUBLISH_PREFIX + "stop.json"
PUBLISH_LEASE_KEY = PUBLISH_PREFIX + "lease.json"
SNAPSHOT_PREFIX = PUBLISH_PREFIX + "snapshots/"
RECEIPT_PREFIX = PUBLISH_PREFIX + "receipts/"
LEDGER_PREFIX = PUBLISH_PREFIX + "generations/"
HEARTBEAT_KEY = PUBLISH_PREFIX + "heartbeat.json"
STATE_SCHEMA = "news-publish-state/v2"            # v2: generations (no deployed v1 state exists)
RECEIPT_SCHEMA = "news-publish-receipt/v1"
SNAPSHOT_SCHEMA = "news-publish-snapshot/v1"
LEDGER_SCHEMA = "news-publish-generation/v1"
PILOT_SCHEMA = "news-pilot-config/v1"
INDEX_CACHE_CONTROL = "no-store"
PAGE_CACHE_CONTROL = "public, max-age=31536000, immutable"
WITHDRAW_NOTE = ("origin objects deleted; copies already cached by browsers or intermediaries, and downloads, "
                 "cannot be recalled")

# Builder output (unqualified) and published generation pages (qualified).
_PAGE_RE = re.compile(r"^news/v1/views/(collector|archive|publication)/(all|ai_buildout|oil)/"
                      r"([0-9]{4})-([0-9]{2})-([0-9]{2})/page-([0-9]{4})\.json$")
_GEN_PAGE_RE = re.compile(rf"^news/v1/views/g/({GEN_PATTERN})/(collector|archive|publication)/(all|ai_buildout|oil)/"
                          r"([0-9]{4})-([0-9]{2})-([0-9]{2})/page-([0-9]{4})\.json$")


class PublishError(ArchiveError):
    pass


class PublishStopped(PublishError):
    """The operator stop marker is present; nothing was written."""


class PublishIntegrityError(PublishError):
    """A public object did not read back as written, or an immutable key holds different bytes."""


class PublishBlocked(PublishError):
    """History is uncertain (unmatched live index or unreadable ledger): nothing written or deleted."""


class PublishPaused(PublishError):
    """Cleanup ran, but a new generation was not admitted (unsettled index write or the page cap)."""


def _real_date(y: str, m: str, d: str) -> bool:
    date = f"{y}-{m}-{d}"
    try:
        return datetime.strptime(date, "%Y-%m-%d").strftime("%Y-%m-%d") == date
    except ValueError:
        return False


def is_builder_view_key(key: str) -> bool:
    """The builder's unqualified keys (index, or mode/stream/real date/page 0001..pages_per_view)."""
    if key == PUBLIC_INDEX:
        return True
    m = _PAGE_RE.match(key)
    return bool(m) and 1 <= int(m.group(6)) <= LIMITS.pages_per_view and _real_date(m.group(3), m.group(4), m.group(5))


def generation_of(key: str) -> str | None:
    """The generation of a published page key, or None if key is not one."""
    m = _GEN_PAGE_RE.match(key)
    if not m or not is_gen(m.group(1)):
        return None
    if not 1 <= int(m.group(7)) <= LIMITS.pages_per_view or not _real_date(m.group(4), m.group(5), m.group(6)):
        return None
    return m.group(1)


def is_public_view_key(key: str) -> bool:
    """The same exact rule as the panel (isNewsViewKey), the proxy (isPublicNewsViewKey) and the gateway."""
    return key == PUBLIC_INDEX or generation_of(key) is not None


# --- the two stores ---------------------------------------------------------------

class PublicViewStore:
    """The public side: refuses any key that is not an exact view key."""

    def __init__(self, inner):
        self.inner = inner

    def _check(self, key):
        if not is_public_view_key(key):
            raise PublishError(f"refusing non-view key on the public store: {key!r}")
        return key

    def put(self, key, data, content_type="application/json", cache_control=None):
        self.inner.put(self._check(key), data, content_type, cache_control=cache_control)

    def put_if_absent(self, key, data, content_type="application/json", cache_control=None) -> bool:
        return self.inner.put_if_absent(self._check(key), data, content_type=content_type, cache_control=cache_control)

    def get(self, key):
        return self.inner.get(self._check(key))

    def delete(self, key):
        self.inner.delete(self._check(key))

    def list_views(self) -> list[str]:
        """Every view key present (other keys under the prefix are reported, never touched)."""
        return [k for k in self.inner.list(PUBLIC_PREFIX) if is_public_view_key(k)]

    def pages_by_gen(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for key in self.inner.list(GENERATION_PREFIX):
            gen = generation_of(key)
            if gen is not None:
                out.setdefault(gen, []).append(key)
        return {g: sorted(v) for g, v in sorted(out.items())}

    def foreign_keys(self) -> list[str]:
        return [k for k in self.inner.list(PUBLIC_PREFIX) if not is_public_view_key(k)]


def store_identity(store) -> tuple:
    if isinstance(store, PublicViewStore):
        store = store.inner
    if isinstance(store, LocalObjectStore):
        return ("local", str(Path(store.root).resolve()))
    if isinstance(store, R2ObjectStore):
        return ("r2", store.bucket)
    return ("object", id(store))


def assert_separate(private, public) -> None:
    """The raw archive and the public views must not share a store (or nest inside each other)."""
    a, b = store_identity(private), store_identity(public)
    if a[0] != b[0]:
        return
    if a == b:
        raise PublishError(f"private and public stores are the same {a[0]} store ({a[1]})")
    if a[0] == "local":
        pa, pb = Path(a[1]), Path(b[1])
        if pa in pb.parents or pb in pa.parents:
            raise PublishError(f"private and public stores nest inside each other ({pa}, {pb})")


# --- prices -----------------------------------------------------------------------

def unavailable_prices() -> PriceFile:
    """No price input connected: every row's price is missing and pages say input=unavailable."""
    return PriceFile(path="unavailable:no-price-input", sha256=sha256_hex(b""),
                     header={"record_kind": "fixture_header", "price_input": "unavailable", "synthetic": False},
                     records=[])


# --- state ------------------------------------------------------------------------

def _read_json(store, key):
    raw = store.get(key)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise PublishError(f"{key} is unreadable: {exc}") from exc


def read_state(private) -> dict:
    state = _read_json(private, STATE_KEY)
    if state is None:
        return {"schema_version": STATE_SCHEMA, "committed": None, "in_progress": None, "last_failure": None,
                "withdrawn": None, "uncertain": None, "blocked": None, "paused": None, "clock": None}
    if state.get("schema_version") != STATE_SCHEMA:
        raise PublishError(f"{STATE_KEY} has schema {state.get('schema_version')!r}")
    return state


def _write_state(private, state) -> None:
    private.put(STATE_KEY, encode_json(state), "application/json")


def stopped(private) -> dict | None:
    return _read_json(private, STOP_KEY)


def stop(private, clock, reason: str) -> dict:
    marker = {"stopped_at": iso(clock()), "reason": reason}
    private.put(STOP_KEY, encode_json(marker), "application/json")
    return marker


def resume(private) -> bool:
    was = private.get(STOP_KEY) is not None
    private.delete(STOP_KEY)
    return was


# --- validation -------------------------------------------------------------------

_PAGE_KEYS = ("data_label", "mode", "as_of", "window", "stream", "date", "page_number", "page_count", "view",
              "complete", "overflow_reasons", "omitted", "coverage", "rows")


def _int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def page_render_problems(page) -> list[str]:
    """The structure the panel's validatePage requires before it renders a page (kept in step by a test)."""
    if not isinstance(page, dict):
        return ["not an object"]
    problems = []
    if page.get("schema_version") != "news-view/v1":
        problems.append("schema_version is not news-view/v1")
    problems += [f"missing {k}" for k in _PAGE_KEYS if k not in page]
    if problems:
        return problems
    for k in ("data_label", "mode", "as_of", "stream", "date"):
        if not isinstance(page[k], str):
            problems.append(f"{k} is not text")
    w = page["window"]
    if not (isinstance(w, dict) and isinstance(w.get("start"), str) and isinstance(w.get("end"), str)):
        problems.append("window is not {start, end}")
    if not (_int(page["page_number"]) and _int(page["page_count"])):
        problems.append("page_number/page_count are not integers")
    if not (isinstance(page["view"], dict) and _int(page["view"].get("rows_total"))):
        problems.append("view is not an object with rows_total")
    if not isinstance(page["complete"], bool):
        problems.append("complete is not true/false")
    if not isinstance(page["overflow_reasons"], list):
        problems.append("overflow_reasons is not a list")
    if not isinstance(page["omitted"], dict):
        problems.append("omitted is not an object")
    cov = page["coverage"]
    if not isinstance(cov, dict):
        problems.append("coverage is not an object")
    else:
        if not (isinstance(cov.get("segments"), list) and all(isinstance(s, dict) for s in cov["segments"])):
            problems.append("coverage.segments is not a list of objects")
        if not isinstance(cov.get("after_cutoff_hindsight"), list):
            problems.append("coverage.after_cutoff_hindsight is not a list")
        if not isinstance(cov.get("price"), dict):
            problems.append("coverage.price is not an object")
    rows = page["rows"]
    if not (isinstance(rows, list) and all(isinstance(r, dict) for r in rows)):
        problems.append("rows is not a list of objects")
    elif rows and not _int(page.get("rows_offset")):
        problems.append("rows_offset is not an integer")
    return problems


# The complete shape the panel's renderer dereferences (M5.2). validatePage is shallow: a page can pass
# it and still make renderPage throw (rows[0].instruments = null, segments[0].sources = {},
# coverage.price.missing = {}). These specs cover every field renderPage, viewPicker, indexBanner and
# mount read, so a generation that passes renders without an exception or an undefined/NaN/null
# placeholder; a sweep in test_news_publish_contract runs the real renderer to keep them in step.
# Spec forms: "str" | "int" (non-negative) | "num" | "bool", "?" suffix = may be null; [spec] = list of
# spec; {name: spec} = object with those fields ("?name" = may be absent); ("?", {...}) = object or
# null; ("map", spec) = object whose values all match spec.
_INTERVAL = {"start": "str", "end": "str", "seconds": "num", "?last_price_time": "str?"}
_SOURCES = {"attempted": ["str"], "ok": ["str"], "failed": [{"source_id": "str", "status": "str"}],
            "not_selected": ["str"], "failed_entries": "int", "disabled_count": "int",
            "roster_complete": "bool", "source_ids_omitted": "int"}
_SEGMENT = {"start": "str?", "end": "str?", "state": "str", "quiet": "bool", "?before_first_run": "bool",
            "?clipped_to_window_start": "bool", "?missed_interval": "bool", "?gap_seconds": "num?",
            "observations_in_span": "int", "events_in_span": "int", "?sources": ("?", _SOURCES)}
_ROW = {
    "revision_id": "str", "source_id": "str", "title": "str", "title_truncated": "bool", "outlet": "str",
    "canonical_url": "str?", "url_rejected": "str?", "event_at": "str", "event_basis": "str",
    "published_at": "str?", "observed_at": "str", "archived_at": "str?",
    "time_quality": {"published": "str", "updated": "str"},
    "revision_number": "int", "revisions_known_at_event": "int?",
    "classification_at_event": ("?", {"timing": "str", "classified_at": "str", "stream_memberships": ["str"]}),
    "instruments": {"mapping": "str"},
    "later_context": {"revisions_after_event": [{"known_at": "str", "title": "str"}],
                      "revisions_after_event_omitted": "int",
                      "classifications_after_event": [{"known_at": "str", "stream_memberships": ["str"]}],
                      "classifications_after_event_omitted": "int", "?stream_membership_removed_at": "str?"},
    "price": {"status": "str", "age_seconds": "num?",
              "record": ("?", {"record_id": "str", "price": "num", "?synthetic": "bool"})},
}
PAGE_SPEC = {
    "data_label": "str", "mode_label": "str", "mode": "str", "as_of": "str", "stream": "str", "date": "str",
    "?hindsight": "bool", "window": {"start": "str", "end": "str"},
    "?price_series": ("?", {"instrument_id": "str", "series": "str", "tolerance_s": "num", "?synthetic": "bool"}),
    "?prev_page": "str?", "?next_page": "str?", "page_number": "int", "page_count": "int", "?rows_offset": "int",
    "?gen": "str",
    "view": {"rows_total": "int", "?rows_omitted": "int", "?pages_omitted": "int"},
    "complete": "bool", "overflow_reasons": ["str"], "omitted": ("map", "int"),
    "coverage": {"coverage_complete": "bool", "stream_scope": "str", "note": "str", "?quiet_blocked_by": ["str"],
                 "segments": [_SEGMENT], "?tail": ("?", {"start": "str", "end": "str", "seconds": "num"}),
                 "after_cutoff_hindsight": [{"end": "str", "state": "str"}],
                 "?pending_availability_runs": "int", "?corrupt_runs": "int", "?uncommitted_runs": "int",
                 "price": {"?missing": [_INTERVAL], "?stale": [_INTERVAL]}},
    "rows": [_ROW],
}
INDEX_SPEC = {
    "data_label": "str", "build_as_of": "str", "complete": "bool", "overflow_reasons": ["str"],
    "views_total": "int", "views_omitted": "int", "?gen": "str", "?pages_sha256": "str",
    "views": [{"mode": "str", "stream": "str", "date": "str", "first_page": "str", "page_count": "int",
               "rows_total": "int", "complete": "bool"}],
}
_SCALARS = {
    "str": lambda v: isinstance(v, str),
    "int": lambda v: _int(v) and v >= 0,
    "num": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float("inf"),
    "bool": lambda v: isinstance(v, bool),
}


def _shape(value, spec, path: str, out: list) -> None:
    if len(out) >= 20:
        return
    if isinstance(spec, str):
        if value is None and spec.endswith("?"):
            return
        kind = spec.rstrip("?")
        if not _SCALARS[kind](value):
            out.append(f"{path} is not {'a non-negative integer' if kind == 'int' else kind}"
                       f"{' or null' if spec.endswith('?') else ''}")
    elif isinstance(spec, list):
        if not isinstance(value, list):
            out.append(f"{path} is not a list")
            return
        for i, item in enumerate(value):
            _shape(item, spec[0], f"{path}[{i}]", out)
    elif isinstance(spec, tuple) and spec[0] == "map":
        if not isinstance(value, dict):
            out.append(f"{path} is not an object")
            return
        for k, item in value.items():
            _shape(item, spec[1], f"{path}.{k}", out)
    elif isinstance(spec, tuple):
        if value is not None:
            _shape(value, spec[1], path, out)
    else:
        if not isinstance(value, dict):
            out.append(f"{path} is not an object")
            return
        for name, sub in spec.items():
            optional = name.startswith("?")
            name = name.lstrip("?")
            if name not in value:
                if not optional:
                    out.append(f"{path}.{name} is missing")
                continue
            _shape(value[name], sub, f"{path}.{name}", out)


def page_contract_problems(page) -> list[str]:
    """Everything the unchanged renderer needs: validatePage's checks, then every nested shape it reads."""
    problems = page_render_problems(page)
    if problems:
        return problems
    out: list[str] = []
    _shape(page, PAGE_SPEC, "page", out)
    return out


def index_contract_problems(index) -> list[str]:
    """Every index field the panel and the publisher read, with its counts consistent."""
    if not isinstance(index, dict) or index.get("schema_version") != "news-view-index/v1":
        return ["index.json is not a news-view-index/v1 document"]
    out: list[str] = []
    _shape(index, INDEX_SPEC, "index", out)
    if out:
        return out
    try:
        parse_iso(index["build_as_of"])
    except (TypeError, ValueError):
        out.append("index.build_as_of is not an ISO-8601 UTC time")
    if index["views_total"] != len(index["views"]) + index["views_omitted"]:
        out.append(f"index.views_total {index['views_total']} != {len(index['views'])} listed + "
                   f"{index['views_omitted']} omitted")
    if index["complete"] != (not index["overflow_reasons"] and index["views_omitted"] == 0):
        out.append("index.complete disagrees with its overflow_reasons/views_omitted")
    reasons = index["overflow_reasons"]
    if reasons != sorted(set(reasons)) or not set(reasons) <= set(INDEX_REASONS):
        out.append(f"index.overflow_reasons {reasons} must be sorted, distinct and from {list(INDEX_REASONS)}")
    if bool(reasons) != (index["views_omitted"] > 0):
        out.append("index.overflow_reasons must name why views are omitted, and only when they are")
    if not index["views"] and index["views_total"] > 0:
        # Cap-induced emptiness: views were built but none fit. Publishing it would replace the last
        # good generation with nothing to show. (views_total 0, no views built at all, stays valid.)
        out.append(f"index lists no views although {index['views_total']} were built (all cut by "
                   f"{', '.join(reasons) or 'its caps'}); refused so the previous publication stays live")
    return out


def _page_key(mode, stream, date, n):
    """The builder's unqualified page key (validate_generation checks builder output)."""
    return page_key(mode, stream, date, n)


def _check_views(files: dict[str, bytes], index: dict, key_of, limits, gen: str | None) -> None:
    """Every page the index names: present, renderable, agreeing with its key, entry and page chain."""
    named, seen_views = set(), set()
    for v in index["views"]:
        ident = (v["mode"], v["stream"], v["date"])
        if ident in seen_views:
            raise PublishError(f"index lists {'/'.join(ident)} twice")
        seen_views.add(ident)
        count = v.get("page_count")
        if not _int(count) or not 1 <= count <= limits.pages_per_view:
            raise PublishError(f"{'/'.join(ident)}: page_count {count!r} is not 1..{limits.pages_per_view}")
        if v.get("first_page") != key_of(*ident, 1):
            raise PublishError(f"{'/'.join(ident)}: first_page {v.get('first_page')!r} is not page-0001 of its view")
        for n in range(1, count + 1):
            key = key_of(*ident, n)
            named.add(key)
            if key not in files:
                raise PublishError(f"index names a page the generation lacks: {key}")
            try:
                page = json.loads(files[key])
            except ValueError as exc:
                raise PublishError(f"{key} is not JSON: {exc}") from exc
            problems = page_contract_problems(page)
            if problems:
                raise PublishError(f"{key} would not render: {'; '.join(problems[:5])}")
            expect = {"mode": ident[0], "stream": ident[1], "date": ident[2], "page_number": n,
                      "page_count": count, "build_as_of": index["build_as_of"],
                      "prev_page": key_of(*ident, n - 1) if n > 1 else None,
                      "next_page": key_of(*ident, n + 1) if n < count else None}
            if gen is not None:
                expect["gen"] = gen
            wrong = {k: page.get(k) for k, want in expect.items() if page.get(k) != want}
            if wrong:
                raise PublishError(f"{key} disagrees with its key, index entry or page chain: {wrong}")
            if page["view"].get("rows_total") != v.get("rows_total") or page["complete"] != v.get("complete"):
                raise PublishError(f"{key}: rows_total/complete differ from its index entry")
    orphans = sorted(set(files) - named - {PUBLIC_INDEX})
    if orphans:
        raise PublishError(f"pages the index does not name: {orphans[:5]}")


def _load_index(files: dict[str, bytes]) -> dict:
    try:
        index = json.loads(files[PUBLIC_INDEX])
    except ValueError as exc:
        raise PublishError(f"index.json is not JSON: {exc}") from exc
    problems = index_contract_problems(index)
    if problems:
        raise PublishError(f"index.json would not render or publish: {'; '.join(problems[:5])}")
    return index


def _check_caps(files: dict[str, bytes], limits) -> None:
    for key, body in files.items():
        cap = limits.index_bytes if key == PUBLIC_INDEX else limits.page_bytes
        if len(body) > cap:
            raise PublishError(f"{key} is {len(body)} bytes, over its {cap}-byte cap")


def validate_generation(files: dict[str, bytes], limits=LIMITS) -> dict:
    """Builder output (unqualified keys), checked before it is qualified and before anything is written.

    Exact keys and byte caps; an index with every field the panel and publisher read
    (index_contract_problems); every page the index names present, renderable by the unchanged
    renderer (page_contract_problems) and agreeing with its key and its index entry; first/previous/
    next links forming exactly the chain page-0001 .. page-<page_count>; no page the index does not name.
    """
    if PUBLIC_INDEX not in files:
        raise PublishError("generation has no index.json")
    bad = sorted(k for k in files if not is_builder_view_key(k))
    if bad:
        raise PublishError(f"generation has non-view keys: {bad[:5]}")
    _check_caps(files, limits)
    index = _load_index(files)
    _check_views(files, index, _page_key, limits, None)
    return index


def validate_qualified(gen: str, files: dict[str, bytes], limits=LIMITS) -> dict:
    """A published-form generation: every key under g/<gen>/, every page and link of that generation,
    index.gen == gen, index.pages_sha256 == the pages' digest, and all of validate_generation's checks."""
    if not is_gen(gen):
        raise PublishError(f"{gen!r} is not a generation id")
    if PUBLIC_INDEX not in files:
        raise PublishError("generation has no index.json")
    bad = sorted(k for k in files if k != PUBLIC_INDEX and generation_of(k) != gen)
    if bad:
        raise PublishError(f"generation {gen} has keys outside news/v1/views/g/{gen}/: {bad[:5]}")
    _check_caps(files, limits)
    index = _load_index(files)
    if index.get("gen") != gen:
        raise PublishError(f"index.gen {index.get('gen')!r} is not {gen}")
    if index.get("pages_sha256") != pages_digest(files):
        raise PublishError("index.pages_sha256 does not match the generation's pages")
    _check_views(files, index, lambda m, s, d, n: generation_page_key(gen, m, s, d, n), limits, gen)
    return index


# Final integrity over every key and byte (snapshots, ledger). The same digest as views.files_digest.
generation_digest = files_digest


def generation_id(files: dict[str, bytes], index: dict | None = None) -> str:
    """The logical generation id of an unqualified build (views.generation_name)."""
    return generation_name(files)


def page_write_order(files: dict[str, bytes]) -> list[str]:
    """Pages only, in key order. Generation pages are immutable and unreachable until the index names
    them, so the order no longer matters to readers; it is fixed only to keep runs reproducible."""
    return sorted(k for k in files if k != PUBLIC_INDEX)


# --- snapshots, receipts, failures --------------------------------------------------

def _snapshot(private, gen_id: str, files: dict[str, bytes], clock) -> None:
    prefix = f"{SNAPSHOT_PREFIX}{gen_id}/"
    manifest_key = prefix + "snapshot.json"
    wanted = {k: {"bytes": len(b), "sha256": sha256_hex(b)} for k, b in sorted(files.items())}
    existing = _read_json(private, manifest_key)
    if existing is not None:
        # The id binds every key and byte, so a match is the same generation; anything else is refused.
        if existing.get("files") != wanted or existing.get("generation_sha256") != generation_digest(files):
            raise PublishError(f"snapshot {gen_id} exists with different content; refusing to reuse it")
        return
    for key, body in sorted(files.items()):
        private.put(prefix + key, body, "application/json")
    private.put(manifest_key, encode_json({
        "schema_version": SNAPSHOT_SCHEMA, "gen_id": gen_id, "created_at": iso(clock()),
        "generation_sha256": generation_digest(files), "files": wanted}), "application/json")


def snapshot_ids(private) -> list[str]:
    ids = set()
    for key in private.list(SNAPSHOT_PREFIX):
        parts = key[len(SNAPSHOT_PREFIX):].split("/")
        if len(parts) == 2 and parts[1] == "snapshot.json":
            ids.add(parts[0])
    return sorted(ids)


def load_snapshot(private, gen_id: str) -> dict[str, bytes]:
    check_key(f"{SNAPSHOT_PREFIX}{gen_id}/snapshot.json")
    meta = _read_json(private, f"{SNAPSHOT_PREFIX}{gen_id}/snapshot.json")
    if meta is None:
        raise PublishError(f"no snapshot {gen_id}; have {snapshot_ids(private)}")
    files = {}
    for key, info in meta["files"].items():
        body = private.get(f"{SNAPSHOT_PREFIX}{gen_id}/{key}")
        if body is None or len(body) != info["bytes"] or sha256_hex(body) != info["sha256"]:
            raise PublishError(f"snapshot {gen_id}: {key} is missing or altered")
        files[key] = body
    # The id's hash part is the logical digest of the unqualified build, so it cannot be recomputed from
    # the published files; the snapshot binds them by generation_sha256 and by the index's own gen.
    digest = generation_digest(files)
    try:
        index_gen = json.loads(files[PUBLIC_INDEX]).get("gen")
    except (KeyError, ValueError, AttributeError):
        index_gen = None
    if meta.get("generation_sha256") != digest or index_gen != gen_id:
        raise PublishError(f"snapshot {gen_id}: its files do not match the generation id")
    return files


def _prune_snapshots(private, keep: int, protect: set[str]) -> list[str]:
    ids = snapshot_ids(private)
    drop = [g for g in ids[: max(0, len(ids) - keep)] if g not in protect]
    for gen in drop:
        prefix = f"{SNAPSHOT_PREFIX}{gen}/"
        manifest = prefix + "snapshot.json"
        for key in private.list(prefix):
            if key != manifest:
                private.delete(key)
        private.delete(manifest)   # last: an interrupted prune leaves a detectable, reloadable-or-failing snapshot
    return drop


def _receipt(private, clock, report: PublishReport, extra: dict) -> str:
    now = clock()
    key = f"{RECEIPT_PREFIX}{now.strftime('%Y%m%dT%H%M%S%fZ')}-{report.action}-{report.gen_id}.json"
    private.put(key, encode_json({"schema_version": RECEIPT_SCHEMA, "at": iso(now), **report.summary(), **extra}),
                "application/json")
    return key


def _record_failure(private, state, stage: str, exc: BaseException, clock) -> None:
    try:
        state["last_failure"] = {"at": iso(clock()), "stage": stage, "error": f"{type(exc).__name__}: {exc}"[:500]}
        _write_state(private, state)
    except Exception:  # the original failure is the one that matters
        pass



# --- publication: generation lifecycle (G3) ---------------------------------------------------------

@dataclass
class PublishReport:
    gen_id: str
    action: str
    written: list[str] = field(default_factory=list)
    unchanged: int = 0
    deleted: list[str] = field(default_factory=list)
    recovered_from: str | None = None
    foreign_keys: list[str] = field(default_factory=list)
    snapshots_pruned: list[str] = field(default_factory=list)
    retired: dict = field(default_factory=dict)        # gen -> why its pages were deleted (orphan / expired)
    public_pages: int = 0

    def summary(self) -> dict:
        return {"gen_id": self.gen_id, "action": self.action, "written": len(self.written),
                "unchanged": self.unchanged, "deleted": len(self.deleted), "recovered_from": self.recovered_from,
                "foreign_keys": self.foreign_keys, "snapshots_pruned": self.snapshots_pruned,
                "retired": self.retired, "public_pages": self.public_pages}


@dataclass(frozen=True)
class GenerationPolicy:
    """Owner-approved defaults (config.GEN_*). clock_bound_verified stays False until a deployment has
    actually verified the store-clock bound; until then expiry deletion is refused (visibly)."""
    reader_grace_s: int = config.GEN_READER_GRACE_S
    clock_epsilon_s: int = config.GEN_CLOCK_EPSILON_S
    clock_skew_s: int = config.GEN_CLOCK_SKEW_S
    page_cap: int = config.GEN_PAGE_CAP
    clock_bound_verified: bool = False

    @property
    def delete_after_s(self) -> int:
        """Earliest deletion after a generation ceases (49 min with the defaults), plus the run delay."""
        return self.reader_grace_s + self.clock_epsilon_s + self.clock_skew_s


DEFAULT_POLICY = GenerationPolicy()
LIFECYCLE_EVENTS = ("published", "restored", "ceased")


def _ledger_key(gen: str) -> str:
    return f"{LEDGER_PREFIX}{gen}.json"


def read_ledger(private) -> tuple[dict[str, dict], list[str]]:
    """(gen -> record, problems). Any problem makes history uncertain: callers retain and stop."""
    records, problems = {}, []
    for key in private.list(LEDGER_PREFIX):
        name = key[len(LEDGER_PREFIX):]
        gen = name[:-5] if name.endswith(".json") else ""
        if not is_gen(gen):
            problems.append(f"{key}: not a generation record")
            continue
        try:
            rec = json.loads(private.get(key))
        except (TypeError, ValueError):
            problems.append(f"{key}: unreadable")
            continue
        if (not isinstance(rec, dict) or rec.get("schema_version") != LEDGER_SCHEMA or rec.get("gen") != gen
                or not isinstance(rec.get("events"), list) or not isinstance(rec.get("page_set"), dict)):
            problems.append(f"{key}: wrong schema, gen or shape")
            continue
        records[gen] = rec
    return records, problems


def _write_ledger(private, rec: dict) -> None:
    private.put(_ledger_key(rec["gen"]), encode_json(rec), "application/json")


def _at(reading: dict) -> dict:
    """The times an event is stamped with: the high-water marks of the reading (never earlier than any
    earlier reading), so a clock that stepped back cannot date a retirement early. No store time for
    this reading means no store stamp, and an event without one is never old enough to delete."""
    stamp = reading.get("stamp") or reading
    return {"producer_at": iso(stamp["producer"]),
            "storage_at": iso(stamp["storage"]) if stamp["storage"] is not None else None}


def _event(private, rec: dict, kind: str, reading: dict, **extra) -> None:
    rec["events"].append({"kind": kind, **_at(reading), **extra})
    _write_ledger(private, rec)


def _kinds(rec: dict) -> list[str]:
    return [e["kind"] for e in rec["events"]]


def _epoch(rec: dict) -> dict | None:
    """The latest published/restored/ceased event. Protection follows the latest epoch, so an old
    restore is not a permanent exemption: a later cease starts a new retirement."""
    for e in reversed(rec["events"]):
        if e["kind"] in LIFECYCLE_EVENTS:
            return e
    return None


def _unsettled_attempt(rec: dict) -> bool:
    """True while the latest index write attempted for this generation has no later published/restored
    event. An older lifecycle event (a cease before a failed restore, say) cannot settle a newer attempt."""
    for e in reversed(rec["events"]):
        if e["kind"] in ("published", "restored"):
            return False
        if e["kind"] == "index_put_attempted":
            return True
    return False


def _new_record(gen: str, files: dict[str, bytes], logical_sha256: str | None) -> dict:
    return {"schema_version": LEDGER_SCHEMA, "gen": gen, "logical_sha256": logical_sha256,
            "pages_sha256": pages_digest(files), "index_sha256": sha256_hex(files[PUBLIC_INDEX]),
            "generation_sha256": files_digest(files),
            "page_set": {k: sha256_hex(b) for k, b in sorted(files.items()) if k != PUBLIC_INDEX},
            "events": []}


def _clock_reading(private, clock, state: dict) -> dict:
    """A paired reading: the producer's time and the store's own time for a heartbeat written now.
    Taken at the start of a run and again after each successful reconciliation that records lifecycle
    events, so a retirement is stamped after the replacement is confirmed live, never at run start.

    A clock that reads earlier than its recorded high-water mark, or a store that reports no
    modification time, is recorded as refused: no expiry deletion this run (retention only grows)."""
    producer = clock()
    private.put(HEARTBEAT_KEY, encode_json({"producer_at": iso(producer)}), "application/json")
    reader = getattr(private, "last_modified", None)
    storage = reader(HEARTBEAT_KEY) if reader is not None else None
    last = state.get("clock") or {}
    refused = []
    last_p = parse_iso(last["producer_at"]) if last.get("producer_at") else None
    last_s = parse_iso(last["storage_at"]) if last.get("storage_at") else None
    if last_p is not None and producer < last_p:
        refused.append("producer clock is earlier than its last recorded reading")
    if storage is None:
        refused.append("the store reports no modification time")
    elif last_s is not None and storage < last_s:
        refused.append("store clock is earlier than its last recorded reading")
    # High-water marks: a reading is never allowed to move them backwards.
    high_p = producer if last_p is None else max(producer, last_p)
    if storage is None:
        high_s = last_s
    else:
        high_s = storage if last_s is None else max(storage, last_s)
    state["clock"] = {
        "producer_at": iso(high_p),
        "storage_at": iso(high_s) if high_s is not None else None,
        "last_reading": {"producer": iso(producer), "storage": iso(storage) if storage is not None else None},
        "refused": refused,
    }
    return {"producer": producer, "storage": storage, "refused": refused,
            "stamp": {"producer": high_p, "storage": high_s if storage is not None else None}}


def _elapsed(event: dict, reading: dict) -> tuple[float | None, float | None]:
    p = (reading["producer"] - parse_iso(event["producer_at"])).total_seconds()
    s = None
    if reading["storage"] is not None and event.get("storage_at"):
        s = (reading["storage"] - parse_iso(event["storage_at"])).total_seconds()
    return p, s


def _time_passed(event: dict, reading: dict, need_s: float, policy: GenerationPolicy) -> tuple[bool, str]:
    """Both clocks show need_s since event, agree within the skew, neither went backwards."""
    if reading["refused"]:
        return False, "clock refused: " + "; ".join(reading["refused"])
    p, s = _elapsed(event, reading)
    if s is None:
        return False, "no store time for this event or run"
    if min(p, s) < need_s:
        return False, f"protected until both clocks show {int(need_s)}s (producer {int(p)}s, store {int(s)}s)"
    if abs(p - s) > policy.clock_skew_s:
        return False, f"producer and store elapsed disagree by {int(abs(p - s))}s (> {policy.clock_skew_s}s skew)"
    return True, ""


def classify(records: dict[str, dict], live_gen: str | None, listed: dict[str, list[str]], reading: dict,
             policy: GenerationPolicy) -> dict[str, dict]:
    """gen -> {"class": current|protected|expired|orphan|uncertain|unknown|gone, "why": ...}.

    Only orphan and expired pages may be deleted. Unknown (public pages with no ledger record) and
    uncertain generations are always retained."""
    out = {}
    for gen in sorted(set(records) | set(listed)):
        rec = records.get(gen)
        if rec is None:
            out[gen] = {"class": "unknown", "why": "public pages with no ledger record: retained"}
            continue
        if gen == live_gen:
            out[gen] = {"class": "current", "why": "named by the live index"}
            continue
        if _unsettled_attempt(rec):
            # Checked before anything else about this generation: an older ceased epoch, a missing page
            # set, elapsed time or an operator note cannot settle a newer attempt. Nothing here can
            # establish that the attempted index write can no longer land.
            out[gen] = {"class": "uncertain",
                        "why": "an index write was attempted and never settled: neither the old index, an older "
                               "lifecycle record nor elapsed time proves it cannot still land; retained, and "
                               "every index write is blocked, until the live index matches it in full"}
            continue
        if gen not in listed:
            out[gen] = {"class": "gone", "why": "no public pages"}
            continue
        epoch = _epoch(rec)
        if epoch is not None and epoch["kind"] == "ceased":
            ok, why = _time_passed(epoch, reading, policy.delete_after_s, policy)
            if ok and not policy.clock_bound_verified:
                ok, why = False, "clock bound unverified: expiry deletion refused"
            out[gen] = {"class": "expired" if ok else "protected", "why": why or "past the reader grace"}
            continue
        if epoch is not None:
            # Recorded current but not the live index: reconciliation ceases these before classify runs.
            out[gen] = {"class": "uncertain", "why": f"ledger says {epoch['kind']} but the live index differs"}
            continue
        # No lifecycle epoch and no index write ever attempted (an attempt is handled above).
        out[gen] = {"class": "orphan", "why": "written, never offered to readers (no index write attempted)"}
    return out


def _live_match(public: "PublicViewStore", records: dict[str, dict]) -> tuple[str | None, str | None, bytes | None]:
    """(gen the live index matches in full, problem, live index bytes). Read-only."""
    live = public.get(PUBLIC_INDEX)
    if live is None:
        return None, None, None
    live_sha = sha256_hex(live)
    try:
        parsed = json.loads(live)
    except ValueError:
        return None, "the live index is not JSON", live
    matches = []
    for rec in records.values():
        if rec["index_sha256"] != live_sha:
            continue
        if not isinstance(parsed, dict) or parsed.get("gen") != rec["gen"] or parsed.get("pages_sha256") != rec["pages_sha256"]:
            continue
        if all((body := public.get(k)) is not None and sha256_hex(body) == h for k, h in rec["page_set"].items()):
            matches.append(rec["gen"])
    if len(matches) != 1:
        return None, ("the live index matches no recorded generation in full (index hash, gen, pages_sha256 and "
                      "every page hash)" if not matches else f"the live index matches several records: {matches}"), live
    return matches[0], None, live


def _reconcile(private, public, state: dict, records: dict[str, dict], reading: dict, fresh=None) -> dict:
    """Settle the ledger against the live index before anything is started or deleted.

    The generation the live index matches (in full) is current: it gets a published/restored event if
    its epoch was not already current, and every other generation recorded as current gets a ceased
    event. Those events are stamped from a reading taken by fresh() AFTER the full index/page match has
    succeeded, so a displaced generation's retirement never starts before its replacement was seen
    live (publication, restore and recovery alike). Anything that cannot be matched blocks the run.
    Returns the reading in force afterwards ("reading")."""
    gen, problem, live = _live_match(public, records)
    if problem:
        return {"live": None, "blocked": problem, "reading": reading}
    if live is None:
        if state.get("committed") is not None:
            return {"live": None, "blocked": "the live index is missing although state records a committed generation",
                    "reading": reading}
        return {"live": None, "blocked": None, "reading": reading}
    rec = records[gen]
    epoch = _epoch(rec)
    becomes_current = epoch is None or epoch["kind"] == "ceased"
    displaced = [o for o in records.values()
                 if o["gen"] != gen and (_epoch(o) or {}).get("kind") in ("published", "restored")]
    if fresh is not None and (becomes_current or displaced or (state.get("committed") or {}).get("gen_id") != gen):
        reading = fresh()          # after the match: never earlier than the moment the index was seen live
    if becomes_current:
        action = (state.get("in_progress") or {}).get("action")
        _event(private, rec, "restored" if (epoch is not None or action == "restore") else "published", reading)
    for other in displaced:
        _event(private, other, "ceased", reading)
    committed = state.get("committed") or {}
    if committed.get("gen_id") != gen:
        index = json.loads(live)
        state["committed"] = {"gen_id": gen, "action": (state.get("in_progress") or {}).get("action", "reconciled"),
                              "build_as_of": index["build_as_of"], "published_at": _at(reading)["producer_at"],
                              "index_sha256": rec["index_sha256"], "generation_sha256": rec["generation_sha256"],
                              "pages": len(rec["page_set"]), "views_total": index["views_total"],
                              "complete": index["complete"], "previous_gen_id": committed.get("gen_id")}
    if (state.get("uncertain") or {}).get("gen") == gen:
        state["uncertain"] = None
    if (state.get("in_progress") or {}).get("gen_id") == gen:
        state["in_progress"] = None
    return {"live": gen, "blocked": None, "reading": reading}


def _cleanup(private, public: "PublicViewStore", records: dict[str, dict], live_gen: str | None, reading: dict,
             policy: GenerationPolicy, report: PublishReport, keep: set) -> dict[str, dict]:
    """Delete orphan and expired generations' pages (never current, protected, uncertain or unknown).
    Runs every time, before admission, and needs no new commit."""
    listed = public.pages_by_gen()
    classes = classify(records, live_gen, listed, reading, policy)
    for gen, info in classes.items():
        if gen in keep or info["class"] not in ("orphan", "expired"):
            continue
        for key in listed.get(gen, []):
            public.delete(key)
            report.deleted.append(key)
        _event(private, records[gen], "pages_deleted", reading, reason=info["class"])
        report.retired[gen] = info["class"]
    return classes


def _run(private, public: "PublicViewStore", gen: str, files: dict[str, bytes], clock, *, action: str,
         policy: GenerationPolicy, keep_snapshots: int, snapshot: bool, logical_sha256: str | None,
         lease_holder=None, break_stale_lease=False) -> PublishReport:
    """Reconcile, clean up, admit, write the generation, commit the index, reconcile again."""
    with WriterLease(private, clock, lease_holder, break_stale=break_stale_lease, key=PUBLISH_LEASE_KEY):
        state = read_state(private)
        report = PublishReport(gen, action)
        if state.get("in_progress"):
            report.recovered_from = state["in_progress"]["gen_id"]
        stage = "clock"
        try:
            reading = _clock_reading(private, clock, state)
            _write_state(private, state)
            stage = "reconcile"
            records, ledger_problems = read_ledger(private)
            if ledger_problems:
                state["blocked"] = {"at": iso(reading["producer"]), "why": "generation ledger: " + "; ".join(ledger_problems[:5])}
                _write_state(private, state)
                raise PublishBlocked(f"history is uncertain ({state['blocked']['why']}); nothing written or deleted")
            reread = lambda: _clock_reading(private, clock, state)   # noqa: E731
            settled = _reconcile(private, public, state, records, reading, reread)
            reading = settled["reading"]
            if settled["blocked"]:
                state["blocked"] = {"at": iso(reading["producer"]), "why": settled["blocked"]}
                _write_state(private, state)
                raise PublishBlocked(f"{settled['blocked']}; nothing written or deleted until an operator settles it")
            state["blocked"] = None
            _write_state(private, state)
            stage = "cleanup"
            classes = _cleanup(private, public, records, settled["live"], reading, policy, report, keep={gen})
            # No exception for the candidate's own generation: retrying it would be a second index write
            # while the first is unresolved, and would not establish that the first has finished.
            uncertain = sorted(g for g, c in classes.items() if c["class"] == "uncertain")
            if uncertain:
                state["paused"] = {"at": iso(reading["producer"]), "why": f"uncertain index write for {uncertain}"}
                _write_state(private, state)
                raise PublishPaused(f"publication paused: generation(s) {uncertain} are uncertain (an index write was "
                                    "never settled); another index write, including a retry of the same "
                                    "generation, could reorder with it. It is settled only when a run finds the "
                                    "live index matching it in full; elapsed time and retries do not release it")
            listed = public.pages_by_gen()
            report.public_pages = sum(len(v) for v in listed.values())
            if settled["live"] == gen:
                state["paused"] = None
                _write_state(private, state)
                report.unchanged = len(files)
                _receipt(private, clock, report, {"note": "already current"})
                return report
            stage = "admission"
            present = set(k for keys in listed.values() for k in keys)
            missing = [k for k in files if k != PUBLIC_INDEX and k not in present]
            if report.public_pages + len(missing) > policy.page_cap:
                state["paused"] = {"at": iso(reading["producer"]),
                                   "why": f"page cap: {report.public_pages} present + {len(missing)} missing > {policy.page_cap}"}
                _write_state(private, state)
                raise PublishPaused(f"publication paused at the page cap ({state['paused']['why']}); protected pages "
                                    "are never evicted, cleanup continues every run")
            state["paused"] = None
            state["in_progress"] = {"gen_id": gen, "action": action, "started_at": iso(reading["producer"]),
                                    "keys": len(files)}
            _write_state(private, state)
            stage = "ledger"
            rec = records.get(gen)
            fresh = _new_record(gen, files, logical_sha256)
            if rec is None:
                rec = fresh
            elif any(rec[k] != fresh[k] for k in ("pages_sha256", "index_sha256", "generation_sha256", "page_set")):
                raise PublishIntegrityError(f"generation {gen} is recorded with different content; refusing to reuse it")
            # Never offered to readers: no index write attempted and never current. Only then may a page of
            # this generation that holds wrong bytes (a failed earlier attempt) be replaced.
            offered = "index_put_attempted" in _kinds(rec) or _epoch(rec) is not None
            _event(private, rec, "candidate", reading, action=action)
            if snapshot:
                stage = "snapshot"
                _snapshot(private, gen, files, clock)
            stage = "pages"
            for key in page_write_order(files):
                body = files[key]
                created = public.put_if_absent(key, body, "application/json", PAGE_CACHE_CONTROL)
                if not created and not offered and public.get(key) != body:
                    public.delete(key)          # our own never-offered leftover: no reader can hold it
                    report.deleted.append(key)
                    created = public.put_if_absent(key, body, "application/json", PAGE_CACHE_CONTROL)
                if public.get(key) != body:
                    raise PublishIntegrityError(f"{key} exists with different bytes" if not created
                                                else f"{key} did not read back as written")
                if created:
                    report.written.append(key)
                else:
                    report.unchanged += 1
            stage = "index"   # the commit point
            _event(private, rec, "index_put_attempted", reading)
            failure = None
            try:
                public.put(PUBLIC_INDEX, files[PUBLIC_INDEX], "application/json", INDEX_CACHE_CONTROL)
                ok = public.get(PUBLIC_INDEX) == files[PUBLIC_INDEX]
            except Exception as exc:   # outcome unknown: the write may still land
                ok, failure = False, exc
            if not ok:
                state["uncertain"] = {"gen": gen, "since": iso(reading["producer"]),
                                      "why": f"{type(failure).__name__}: {failure}"[:300] if failure else "index did not read back"}
                _write_state(private, state)
                raise PublishIntegrityError(f"index write for {gen} is uncertain; the next run reconciles before "
                                            "anything else")
            report.written.append(PUBLIC_INDEX)
            stage = "settle"
            records, _ = read_ledger(private)
            # The reading from the start of the run predates the upload: the displaced generation is
            # retired from a fresh reading taken inside _reconcile, after the full match succeeds.
            settled = _reconcile(private, public, state, records, reading, reread)
            reading = settled["reading"]
            if settled["live"] != gen:
                state["uncertain"] = {"gen": gen, "since": iso(reading["producer"]),
                                      "why": settled["blocked"] or "the live index does not match the candidate"}
                _write_state(private, state)
                raise PublishIntegrityError(f"index for {gen} read back but did not reconcile: {state['uncertain']['why']}")
            state["last_failure"] = None
            state["withdrawn"] = None
            _write_state(private, state)
            stage = "receipt"
            report.public_pages = sum(len(v) for v in public.pages_by_gen().values())
            if snapshot:
                previous = state["committed"].get("previous_gen_id")
                report.snapshots_pruned = _prune_snapshots(private, keep_snapshots, {gen, previous} - {None})
            _receipt(private, clock, report, {"build_as_of": state["committed"]["build_as_of"]})
        except (PublishBlocked, PublishPaused):
            raise
        except BaseException as exc:
            _record_failure(private, state, stage, exc, clock)
            raise
        return report


def publish(private, public, files: dict[str, bytes], clock, *, keep_snapshots: int = 3,
            policy: GenerationPolicy = DEFAULT_POLICY, lease_holder=None, break_stale_lease=False) -> PublishReport:
    """Publish builder output: validate, qualify into an immutable generation, then _run."""
    public = public if isinstance(public, PublicViewStore) else PublicViewStore(public)
    assert_separate(private, public)
    if stopped(private) is not None:
        raise PublishStopped(f"publication is stopped ({stopped(private).get('reason', '')}); run `resume` first")
    validate_generation(files)
    gen, qualified = qualify_generation(files)
    validate_qualified(gen, qualified)
    return _run(private, public, gen, qualified, clock, action="publish", policy=policy,
                keep_snapshots=keep_snapshots, snapshot=True, logical_sha256=files_digest(files),
                lease_holder=lease_holder, break_stale_lease=break_stale_lease)


def restore(private, public, gen_id: str, clock, *, policy: GenerationPolicy = DEFAULT_POLICY,
            lease_holder=None, break_stale_lease=False) -> PublishReport:
    """Republish a retained snapshot byte-for-byte. Requires the stop marker. Counts missing pages
    against the cap like a publication; the displaced generation's retirement starts now."""
    public = public if isinstance(public, PublicViewStore) else PublicViewStore(public)
    assert_separate(private, public)
    if stopped(private) is None:
        raise PublishError("restore requires `stop` first, so the next cycle does not republish over it")
    files = load_snapshot(private, gen_id)
    validate_qualified(gen_id, files)
    return _run(private, public, gen_id, files, clock, action="restore", policy=policy, keep_snapshots=0,
                snapshot=False, logical_sha256=None, lease_holder=lease_holder, break_stale_lease=break_stale_lease)


def withdraw(private, public, clock, *, lease_holder=None, break_stale_lease=False) -> dict:
    """Explicit withdrawal: delete the origin index, then every generation's pages, at once (no grace).
    Cached copies and downloads made before this cannot be recalled."""
    public = public if isinstance(public, PublicViewStore) else PublicViewStore(public)
    assert_separate(private, public)
    if stopped(private) is None:
        raise PublishError("withdraw requires `stop` first, so the next cycle does not republish")
    with WriterLease(private, clock, lease_holder, break_stale=break_stale_lease, key=PUBLISH_LEASE_KEY):
        state = read_state(private)
        reading = _clock_reading(private, clock, state)
        records, _problems = read_ledger(private)   # withdrawal deletes everything public regardless
        deleted = []
        if public.get(PUBLIC_INDEX) is not None:
            public.delete(PUBLIC_INDEX)
            deleted.append(PUBLIC_INDEX)
        for gen, keys in sorted(public.pages_by_gen().items()):
            for key in keys:
                public.delete(key)
                deleted.append(key)
            rec = records.get(gen)
            if rec is not None:
                if (_epoch(rec) or {}).get("kind") in ("published", "restored"):
                    _event(private, rec, "ceased", reading, reason="withdrawn")
                _event(private, rec, "pages_deleted", reading, reason="withdrawn")
        gen = (state.get("committed") or {}).get("gen_id")
        state["withdrawn"] = {"at": iso(clock()), "gen_id": gen, "deleted": len(deleted)}
        state["committed"] = None
        state["in_progress"] = None
        _write_state(private, state)
        report = PublishReport(gen or "none", "withdraw", deleted=deleted)
        _receipt(private, clock, report, {"note": WITHDRAW_NOTE})
        return {"withdrawn_gen_id": gen, "deleted": len(deleted), "snapshots_kept": snapshot_ids(private),
                "uncertain": state.get("uncertain"), "note": WITHDRAW_NOTE}


# --- operating status -------------------------------------------------------------

def status(private, public, clock, *, interval_s: int = config.INTERVAL_S,
           budget_s: int = config.CYCLE_BUDGET_S, policy: GenerationPolicy = DEFAULT_POLICY) -> dict:
    """What an operator checks. Read-only: no heartbeat, so expiry is shown on the producer clock only."""
    public = public if isinstance(public, PublicViewStore) else PublicViewStore(public)
    state = read_state(private)
    now = clock()
    committed = state.get("committed")
    age = None
    if committed:
        age = (now - parse_iso(committed["published_at"])).total_seconds()
    stale_after = 2 * interval_s + budget_s
    live_index = public.get(PUBLIC_INDEX)
    problems = []
    if state.get("last_failure"):
        problems.append("last_publication_failed")
    if state.get("in_progress"):
        problems.append("publication_interrupted")
    if state.get("blocked"):
        problems.append("publication_blocked")
    if state.get("paused"):
        problems.append("publication_paused")
    if state.get("uncertain"):
        problems.append("uncertain_index_write")
    if committed and age is not None and age > stale_after and not stopped(private):
        problems.append("published_views_stale")
    if committed and (live_index is None or sha256_hex(live_index) != committed["index_sha256"]):
        problems.append("public_index_differs_from_state")
    if committed is None and not state.get("withdrawn") and not stopped(private):
        problems.append("never_published")
    records, ledger_problems = read_ledger(private)
    if ledger_problems:
        problems.append("ledger_unreadable")
    live_gen, live_problem, _ = _live_match(public, records)
    listed = public.pages_by_gen()
    reading = {"producer": now, "storage": None, "refused": ["status is read-only (no store time)"]}
    classes = classify(records, live_gen, listed, reading, policy)
    generations = {}
    waiting_on_clock_bound = False
    for gen, info in classes.items():
        entry = dict(info, pages=len(listed.get(gen, [])))
        epoch = _epoch(records[gen]) if gen in records else None
        if epoch is not None and epoch["kind"] == "ceased" and gen in listed:
            earliest = parse_iso(epoch["producer_at"]) + timedelta(seconds=policy.delete_after_s)
            entry["earliest_deletion_producer_clock"] = iso(earliest)
            if now >= earliest and not policy.clock_bound_verified:
                waiting_on_clock_bound = True
        generations[gen] = entry
    if waiting_on_clock_bound:
        problems.append("expiry_blocked_clock_bound_unverified")
    if any(c["class"] == "unknown" for c in classes.values()):
        problems.append("unknown_public_generations")
    if live_problem:
        problems.append("live_index_unmatched")
    leases = {}
    for name, key in (("writer", LEASE_KEY), ("publish", PUBLISH_LEASE_KEY)):
        lease = _read_json(private, key)
        if lease is None:
            continue
        expires = parse_iso(lease.get("expires_at")) if lease.get("expires_at") else None
        expired = expires is None or expires <= now
        leases[name] = {"holder": lease.get("holder"), "acquired_at": lease.get("acquired_at"),
                        "expires_at": lease.get("expires_at"), "expired": expired}
        if expired:
            # Left behind by a process that did not exit cleanly. Never taken over automatically: the
            # operator confirms no writer or publisher runs, then passes --break-stale-lease.
            problems.append(f"stale_{name}_lease")
    public_pages = sum(len(v) for v in listed.values())
    return {"now": iso(now), "stopped": stopped(private), "committed": committed, "leases": leases,
            "published_age_s": age, "stale_after_s": stale_after, "in_progress": state.get("in_progress"),
            "last_failure": state.get("last_failure"), "withdrawn": state.get("withdrawn"),
            "blocked": state.get("blocked"), "paused": state.get("paused"), "uncertain": state.get("uncertain"),
            "clock": state.get("clock"), "live_generation": live_gen, "generations": generations,
            "public_pages": public_pages, "page_cap": policy.page_cap,
            "policy": {"reader_grace_s": policy.reader_grace_s, "delete_after_s": policy.delete_after_s,
                       "clock_bound_verified": policy.clock_bound_verified},
            "snapshots": snapshot_ids(private), "public_foreign_keys": public.foreign_keys(),
            "healthy": not problems, "problems": problems}


# --- the pilot cycle --------------------------------------------------------------

@dataclass(frozen=True)
class PilotConfig:
    raw: dict

    @property
    def active(self) -> bool:
        return self.raw["active"] is True

    def __getitem__(self, key):
        return self.raw[key]


def load_pilot_config(path) -> PilotConfig:
    cfg = json.loads(Path(path).read_bytes())
    problems = []
    if cfg.get("schema_version") != PILOT_SCHEMA:
        problems.append(f"schema_version must be {PILOT_SCHEMA}")
    if cfg.get("active") not in (True, False):
        problems.append("active must be true or false")
    sched = cfg.get("schedule") or {}
    if sched.get("interval_s") != config.INTERVAL_S or sched.get("cron") != "*/15 * * * *":
        problems.append("the pilot cadence is 15 minutes (interval_s 900, cron */15 * * * *)")
    if sched.get("timezone") != "UTC":
        problems.append("schedule.timezone must be UTC")
    if sched.get("restart_policy") != "NEVER":
        problems.append("schedule.restart_policy must be NEVER (one-shot cycles)")
    if not 0 < (cfg.get("cycle_budget_s") or 0) <= config.CYCLE_BUDGET_S:
        problems.append(f"cycle_budget_s must be 1..{config.CYCLE_BUDGET_S}")
    enabled = sorted(s.id for s in config.SOURCES.values() if s.enabled)
    if sorted(cfg.get("sources") or []) != enabled:
        problems.append(f"sources must be exactly the four enabled sources {enabled}")
    if sorted(cfg.get("streams") or []) != sorted(config.STREAMS):
        problems.append(f"streams must be {sorted(config.STREAMS)}")
    views = cfg.get("views") or {}
    if not 1 <= (views.get("days") or 0) <= 14:
        problems.append("views.days must be 1..14")
    if not (views.get("tolerance_s") or 0) > 0:
        problems.append("views.tolerance_s must be positive")
    if not views.get("data_label"):
        problems.append("views.data_label is required")
    if (cfg.get("prices") or {}).get("input") != "unavailable":
        problems.append("prices.input must be 'unavailable' until a real price input is connected")
    stores = cfg.get("storage") or {}
    priv, pub = stores.get("private") or {}, stores.get("public") or {}
    if not priv.get("env_prefix") or not pub.get("env_prefix") or priv.get("env_prefix") == pub.get("env_prefix"):
        problems.append("storage.private and storage.public need distinct env_prefix values")
    if pub.get("prefix") != PUBLIC_PREFIX:
        problems.append(f"storage.public.prefix must be {PUBLIC_PREFIX}")
    if not 1 <= ((cfg.get("retention") or {}).get("snapshots") or 0) <= 10:
        problems.append("retention.snapshots must be 1..10")
    gens = cfg.get("generations") or {}
    reader_floor = config.GEN_READER_DEADLINE_S + config.GEN_INDEX_LEASE_S + config.GEN_READER_DEADLINE_S
    if not isinstance(gens.get("reader_grace_s"), int) or gens["reader_grace_s"] < reader_floor:
        problems.append(f"generations.reader_grace_s must be an integer >= {reader_floor} "
                        "(index age + index lease + page request deadline)")
    for name in ("clock_epsilon_s", "clock_skew_s"):
        if not isinstance(gens.get(name), int) or gens[name] <= 0:
            problems.append(f"generations.{name} must be a positive integer")
    per_generation = 7 * len(STREAM_KEYS) * len(MODES) * LIMITS.pages_per_view
    if not isinstance(gens.get("page_cap"), int) or gens["page_cap"] < per_generation:
        problems.append(f"generations.page_cap must be an integer >= {per_generation} (one pilot generation at its maximum)")
    if gens.get("clock_bound_verified") not in (True, False):
        problems.append("generations.clock_bound_verified must be true or false")
    if problems:
        raise PublishError("pilot config invalid: " + "; ".join(problems))
    return PilotConfig(cfg)


def policy_from(cfg: PilotConfig) -> GenerationPolicy:
    g = cfg["generations"]
    return GenerationPolicy(reader_grace_s=g["reader_grace_s"], clock_epsilon_s=g["clock_epsilon_s"],
                            clock_skew_s=g["clock_skew_s"], page_cap=g["page_cap"],
                            clock_bound_verified=g["clock_bound_verified"] is True)


def bucket_store_from_env(env, prefix: str) -> R2ObjectStore:
    names = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME")
    return R2ObjectStore.from_env({n: env.get(prefix + n, "") for n in names})


def view_dates_for(now: datetime, days: int) -> list[str]:
    today = now.astimezone(timezone.utc).date()
    return [(today - timedelta(days=n)).isoformat() for n in range(days - 1, -1, -1)]


def pilot_cycle(private, public, spool, fetch, clock, cfg: PilotConfig, *, commit_clock=None,
                lease_holder=None, break_stale_lease=False) -> dict:
    """One scheduled cycle: collect the four sources, build bounded views, publish. A stop marker no-ops."""
    public = public if isinstance(public, PublicViewStore) else PublicViewStore(public)
    assert_separate(private, public)
    marker = stopped(private)
    if marker is not None:
        return {"status": "stopped", "stop": marker}
    report = collect_once(private, spool, fetch, clock, streams=list(cfg["streams"]),
                          only_sources=list(cfg["sources"]), budget_s=cfg["cycle_budget_s"],
                          commit_clock=commit_clock, lease_holder=lease_holder,
                          break_stale_lease=break_stale_lease)
    now = (commit_clock or clock)()
    v = cfg["views"]
    # qualified_limits leaves room for the generation-qualified keys and fields added on publication.
    files = build_views(load_archive(private), unavailable_prices(), as_of=now, tolerance_s=v["tolerance_s"],
                        data_label=v["data_label"], dates=view_dates_for(now, v["days"]), limits=qualified_limits())
    pub = publish(private, public, files, commit_clock or clock, keep_snapshots=cfg["retention"]["snapshots"],
                  policy=policy_from(cfg), lease_holder=lease_holder, break_stale_lease=break_stale_lease)
    m = report.manifest
    return {"status": "published", "run": {"run_id": report.run_id, "status": m["status"],
                                           "committed_at": m["committed_at"], "counts": m["counts"],
                                           "index_error": report.index_error, "history": report.checkpoint},
            "publication": pub.summary()}


__all__ = ["DEFAULT_POLICY", "GenerationPolicy", "MODES", "STREAM_KEYS", "PublicViewStore", "PublishBlocked",
           "PublishError", "PublishIntegrityError", "PublishPaused", "PublishStopped", "assert_separate", "classify",
           "generation_of", "is_builder_view_key", "is_public_view_key", "load_pilot_config", "pilot_cycle",
           "policy_from", "publish", "read_ledger", "read_state", "restore", "resume", "status", "stop",
           "unavailable_prices", "validate_generation", "validate_qualified", "withdraw"]
