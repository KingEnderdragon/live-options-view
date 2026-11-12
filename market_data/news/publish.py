"""Publish sanitized news views (M5, local preparation): private archive -> public views.

Two stores, never the same one:

  private   the raw archive (news/v1/runs/, indexes, latest.json, writer lease)
            plus the publisher's own state under news/v1/publish/:
              state.json                  committed generation, in-progress target, last failure
              stop.json                   operator stop marker (cycles and publication no-op)
              lease.json                  single-publisher lease
              snapshots/<gen>/...         exact bytes of recent published generations (restore)
              receipts/<time>-<gen>.json  one per publication / restore / withdrawal
  public    only the exact view keys the panel and proxy accept:
              news/v1/views/index.json
              news/v1/views/<collector|archive|publication>/<all|ai_buildout|oil>/<YYYY-MM-DD>/page-NNNN.json

A publication replaces one generation with the next in this order:

  1. validate every file: exact keys, sizes, an index the panel accepts, and every
     named page renderable, matching its key/index entry, with prev/next forming the
     exact page-0001..page-N chain (nothing is written if anything fails);
  2. record the in-progress target in private state, then snapshot the bytes privately
     under a gen_id that binds every key and byte;
  3. write each changed page, highest page number first within a view, reading each
     back (unchanged pages are not rewritten);
  4. write index.json last and read it back: this is the commit point;
  5. only then delete public view keys the new generation does not contain;
  6. record the committed generation and a receipt; prune old snapshots.

After any prefix of these writes every page reachable from the live index exists
and renders (no page links to one not yet written). A multi-page view can show
pages of two builds until the commit or the next cycle; each carries its own
build_as_of (generation-specific keys would remove this; see PUBLISHING.md). The
failure is recorded in state.json and the next cycle republishes and removes
leftovers. Expired leases are reported, never taken over without
--break-stale-lease. Nothing here contacts a
network or a bucket unless a caller passes such a store.
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
from .views import INDEX_REASONS, LIMITS, ROOT, STREAM_KEYS, build_views

PUBLIC_PREFIX = f"{ROOT}/"
PUBLIC_INDEX = f"{ROOT}/index.json"
PUBLISH_PREFIX = f"{config.NAMESPACE}/publish/"
STATE_KEY = PUBLISH_PREFIX + "state.json"
STOP_KEY = PUBLISH_PREFIX + "stop.json"
PUBLISH_LEASE_KEY = PUBLISH_PREFIX + "lease.json"
SNAPSHOT_PREFIX = PUBLISH_PREFIX + "snapshots/"
RECEIPT_PREFIX = PUBLISH_PREFIX + "receipts/"
STATE_SCHEMA = "news-publish-state/v1"
RECEIPT_SCHEMA = "news-publish-receipt/v1"
SNAPSHOT_SCHEMA = "news-publish-snapshot/v1"
PILOT_SCHEMA = "news-pilot-config/v1"

_PAGE_RE = re.compile(r"^news/v1/views/(collector|archive|publication)/(all|ai_buildout|oil)/"
                      r"([0-9]{4})-([0-9]{2})-([0-9]{2})/page-([0-9]{4})\.json$")


class PublishError(ArchiveError):
    pass


class PublishStopped(PublishError):
    """The operator stop marker is present; nothing was written."""


class PublishIntegrityError(PublishError):
    """A public object did not read back as written."""


def is_public_view_key(key: str) -> bool:
    """The same exact rule as the panel (isNewsViewKey) and the proxy (isPublicNewsViewKey)."""
    if key == PUBLIC_INDEX:
        return True
    m = _PAGE_RE.match(key)
    if not m:
        return False
    if not 1 <= int(m.group(6)) <= LIMITS.pages_per_view:
        return False
    date = f"{m.group(3)}-{m.group(4)}-{m.group(5)}"
    try:
        return datetime.strptime(date, "%Y-%m-%d").strftime("%Y-%m-%d") == date
    except ValueError:
        return False


# --- the two stores ---------------------------------------------------------------

class PublicViewStore:
    """The public side: refuses any key that is not an exact view key."""

    def __init__(self, inner):
        self.inner = inner

    def _check(self, key):
        if not is_public_view_key(key):
            raise PublishError(f"refusing non-view key on the public store: {key!r}")
        return key

    def put(self, key, data, content_type="application/json"):
        self.inner.put(self._check(key), data, content_type)

    def get(self, key):
        return self.inner.get(self._check(key))

    def delete(self, key):
        self.inner.delete(self._check(key))

    def list_views(self) -> list[str]:
        """Every view key present (other keys under the prefix are reported, never touched)."""
        return [k for k in self.inner.list(PUBLIC_PREFIX) if is_public_view_key(k)]

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
                "withdrawn": None}
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
    "views_total": "int", "views_omitted": "int",
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
    return f"{ROOT}/{mode}/{stream}/{date}/page-{n:04d}.json"


def validate_generation(files: dict[str, bytes], limits=LIMITS) -> dict:
    """Every file checked before anything is written.

    Exact keys and byte caps; an index with every field the panel and publisher
    read (index_contract_problems); every page the index names present,
    renderable by the unchanged renderer (page_contract_problems: validatePage's
    checks plus every nested shape renderPage reads) and agreeing with its key and
    its index entry; first/previous/next links forming exactly the chain
    page-0001 .. page-<page_count>; no page that the index does not name.
    """
    if PUBLIC_INDEX not in files:
        raise PublishError("generation has no index.json")
    bad = sorted(k for k in files if not is_public_view_key(k))
    if bad:
        raise PublishError(f"generation has non-view keys: {bad[:5]}")
    for key, body in files.items():
        cap = limits.index_bytes if key == PUBLIC_INDEX else limits.page_bytes
        if len(body) > cap:
            raise PublishError(f"{key} is {len(body)} bytes, over its {cap}-byte cap")
    try:
        index = json.loads(files[PUBLIC_INDEX])
    except ValueError as exc:
        raise PublishError(f"index.json is not JSON: {exc}") from exc
    problems = index_contract_problems(index)
    if problems:
        raise PublishError(f"index.json would not render or publish: {'; '.join(problems[:5])}")
    named, seen_views = set(), set()
    for v in index["views"]:
        ident = (v["mode"], v["stream"], v["date"])
        if ident in seen_views:
            raise PublishError(f"index lists {'/'.join(ident)} twice")
        seen_views.add(ident)
        count = v.get("page_count")
        if not _int(count) or not 1 <= count <= limits.pages_per_view:
            raise PublishError(f"{'/'.join(ident)}: page_count {count!r} is not 1..{limits.pages_per_view}")
        if v.get("first_page") != _page_key(*ident, 1):
            raise PublishError(f"{'/'.join(ident)}: first_page {v.get('first_page')!r} is not page-0001 of its view")
        for n in range(1, count + 1):
            key = _page_key(*ident, n)
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
                      "prev_page": _page_key(*ident, n - 1) if n > 1 else None,
                      "next_page": _page_key(*ident, n + 1) if n < count else None}
            wrong = {k: page.get(k) for k, want in expect.items() if page.get(k) != want}
            if wrong:
                raise PublishError(f"{key} disagrees with its key, index entry or page chain: {wrong}")
            if page["view"].get("rows_total") != v.get("rows_total") or page["complete"] != v.get("complete"):
                raise PublishError(f"{key}: rows_total/complete differ from its index entry")
    orphans = sorted(set(files) - named - {PUBLIC_INDEX})
    if orphans:
        raise PublishError(f"pages the index does not name: {orphans[:5]}")
    return index


def generation_digest(files: dict[str, bytes]) -> str:
    """SHA-256 over every key and its content bytes (length-prefixed, sorted by key)."""
    import hashlib

    h = hashlib.sha256()
    for key in sorted(files):
        body = files[key]
        h.update(f"{len(key)}:{key}:{len(body)}:".encode("utf-8"))
        h.update(body)
    return h.hexdigest()


def generation_id(files: dict[str, bytes], index: dict) -> str:
    built = parse_iso(index["build_as_of"])
    return f"{built.strftime('%Y%m%dT%H%M%SZ')}-{generation_digest(files)[:16]}"


def page_write_order(files: dict[str, bytes]) -> list[str]:
    """Pages only, highest page number first within each view.

    With fixed keys a page is visible as soon as it is written, so a page must
    never link to a page that is not yet there. Writing page N before page N-1
    means every link reachable from the live index (old or new) resolves after
    any prefix of the writes. It does not make a multi-page view switch builds
    atomically; that needs generation-specific keys (see PUBLISHING.md).
    """
    pages = [k for k in files if k != PUBLIC_INDEX]
    return sorted(pages, key=lambda k: (k.rsplit("/", 1)[0], -int(k[-9:-5])))


# --- publication ------------------------------------------------------------------

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

    def summary(self) -> dict:
        return {"gen_id": self.gen_id, "action": self.action, "written": len(self.written),
                "unchanged": self.unchanged, "deleted": len(self.deleted), "recovered_from": self.recovered_from,
                "foreign_keys": self.foreign_keys, "snapshots_pruned": self.snapshots_pruned}


def _put_verified(public: PublicViewStore, key: str, body: bytes) -> None:
    public.put(key, body, "application/json")
    if public.get(key) != body:
        raise PublishIntegrityError(f"{key} did not read back as written")


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
    digest = generation_digest(files)
    if meta.get("generation_sha256") != digest or not gen_id.endswith("-" + digest[:16]):
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


def _replace(private, public: PublicViewStore, files: dict[str, bytes], clock, *, action: str,
             keep_snapshots: int, snapshot: bool, lease_holder=None, break_stale_lease=False,
             allow_stopped=False) -> PublishReport:
    assert_separate(private, public)
    if not allow_stopped and stopped(private) is not None:
        raise PublishStopped(f"publication is stopped ({stopped(private).get('reason', '')}); run `resume` first")
    index = validate_generation(files)
    gen_id = generation_id(files, index)
    with WriterLease(private, clock, lease_holder, break_stale=break_stale_lease, key=PUBLISH_LEASE_KEY):
        state = read_state(private)
        report = PublishReport(gen_id, action)
        if state.get("in_progress"):
            report.recovered_from = state["in_progress"]["gen_id"]
        stage = "start"
        try:
            state["in_progress"] = {"gen_id": gen_id, "action": action, "started_at": iso(clock()),
                                    "keys": len(files)}
            _write_state(private, state)
            if snapshot:
                stage = "snapshot"
                _snapshot(private, gen_id, files, clock)
            stage = "pages"
            for key in page_write_order(files):
                if public.get(key) == files[key]:
                    report.unchanged += 1
                    continue
                _put_verified(public, key, files[key])
                report.written.append(key)
            stage = "index"   # the commit point
            if public.get(PUBLIC_INDEX) == files[PUBLIC_INDEX]:
                report.unchanged += 1
            else:
                _put_verified(public, PUBLIC_INDEX, files[PUBLIC_INDEX])
                report.written.append(PUBLIC_INDEX)
            stage = "cleanup"
            report.foreign_keys = public.foreign_keys()
            for key in public.list_views():
                if key not in files:
                    public.delete(key)
                    report.deleted.append(key)
            stage = "state"
            previous = (state.get("committed") or {}).get("gen_id")
            state["committed"] = {"gen_id": gen_id, "action": action, "build_as_of": index["build_as_of"],
                                  "published_at": iso(clock()), "index_sha256": sha256_hex(files[PUBLIC_INDEX]),
                                  "generation_sha256": generation_digest(files),
                                  "pages": len(files) - 1, "views_total": index["views_total"],
                                  "complete": index["complete"], "previous_gen_id": previous}
            state["in_progress"] = None
            state["last_failure"] = None
            state["withdrawn"] = None
            _write_state(private, state)
            stage = "receipt"
            if snapshot:
                report.snapshots_pruned = _prune_snapshots(private, keep_snapshots, {gen_id, previous} - {None})
            _receipt(private, clock, report, {"build_as_of": index["build_as_of"]})
        except BaseException as exc:
            _record_failure(private, state, stage, exc, clock)
            raise
        return report


def publish(private, public, files: dict[str, bytes], clock, *, keep_snapshots: int = 3,
            lease_holder=None, break_stale_lease=False) -> PublishReport:
    """Replace the public generation with `files` (pages first, index last, stale pages after)."""
    public = public if isinstance(public, PublicViewStore) else PublicViewStore(public)
    return _replace(private, public, files, clock, action="publish", keep_snapshots=keep_snapshots,
                    snapshot=True, lease_holder=lease_holder, break_stale_lease=break_stale_lease)


def restore(private, public, gen_id: str, clock, *, lease_holder=None, break_stale_lease=False) -> PublishReport:
    """Republish a retained snapshot byte-for-byte. Requires the stop marker, so a cycle cannot overwrite it."""
    public = public if isinstance(public, PublicViewStore) else PublicViewStore(public)
    if stopped(private) is None:
        raise PublishError("restore requires `stop` first, so the next cycle does not republish over it")
    files = load_snapshot(private, gen_id)
    return _replace(private, public, files, clock, action="restore", keep_snapshots=0, snapshot=False,
                    lease_holder=lease_holder, break_stale_lease=break_stale_lease, allow_stopped=True)


def withdraw(private, public, clock, *, lease_holder=None, break_stale_lease=False) -> dict:
    """Take the views down: index.json first (the panel then reports none published), then every page."""
    public = public if isinstance(public, PublicViewStore) else PublicViewStore(public)
    assert_separate(private, public)
    if stopped(private) is None:
        raise PublishError("withdraw requires `stop` first, so the next cycle does not republish")
    with WriterLease(private, clock, lease_holder, break_stale=break_stale_lease, key=PUBLISH_LEASE_KEY):
        state = read_state(private)
        deleted = []
        if public.get(PUBLIC_INDEX) is not None:
            public.delete(PUBLIC_INDEX)
            deleted.append(PUBLIC_INDEX)
        for key in public.list_views():
            public.delete(key)
            deleted.append(key)
        gen = (state.get("committed") or {}).get("gen_id")
        state["withdrawn"] = {"at": iso(clock()), "gen_id": gen, "deleted": len(deleted)}
        state["committed"] = None
        state["in_progress"] = None
        _write_state(private, state)
        report = PublishReport(gen or "none", "withdraw", deleted=deleted)
        _receipt(private, clock, report, {})
        return {"withdrawn_gen_id": gen, "deleted": len(deleted), "snapshots_kept": snapshot_ids(private)}


# --- operating status -------------------------------------------------------------

def status(private, public, clock, *, interval_s: int = config.INTERVAL_S,
           budget_s: int = config.CYCLE_BUDGET_S) -> dict:
    """What an operator checks: published age, in-progress/failed attempts, stop, leftovers."""
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
    if committed and age is not None and age > stale_after and not stopped(private):
        problems.append("published_views_stale")
    if committed and (live_index is None or sha256_hex(live_index) != committed["index_sha256"]):
        problems.append("public_index_differs_from_state")
    if committed is None and not state.get("withdrawn") and not stopped(private):
        problems.append("never_published")
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
    return {"now": iso(now), "stopped": stopped(private), "committed": committed, "leases": leases,
            "published_age_s": age, "stale_after_s": stale_after, "in_progress": state.get("in_progress"),
            "last_failure": state.get("last_failure"), "withdrawn": state.get("withdrawn"),
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
    if problems:
        raise PublishError("pilot config invalid: " + "; ".join(problems))
    return PilotConfig(cfg)


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
    files = build_views(load_archive(private), unavailable_prices(), as_of=now, tolerance_s=v["tolerance_s"],
                        data_label=v["data_label"], dates=view_dates_for(now, v["days"]))
    pub = publish(private, public, files, commit_clock or clock, keep_snapshots=cfg["retention"]["snapshots"],
                  lease_holder=lease_holder, break_stale_lease=break_stale_lease)
    m = report.manifest
    return {"status": "published", "run": {"run_id": report.run_id, "status": m["status"],
                                           "committed_at": m["committed_at"], "counts": m["counts"],
                                           "index_error": report.index_error, "history": report.checkpoint},
            "publication": pub.summary()}


__all__ = ["MODES", "STREAM_KEYS", "PublicViewStore", "PublishError", "PublishIntegrityError", "PublishStopped",
           "assert_separate", "is_public_view_key", "load_pilot_config", "pilot_cycle", "publish", "read_state",
           "restore", "resume", "status", "stop", "unavailable_prices", "validate_generation", "withdraw"]
