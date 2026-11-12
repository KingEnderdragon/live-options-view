# MediaFlow news pilot: publishing and operating plan (M5, prepared, inactive)

This describes the prepared pilot. It is not active. `deploy/news-pilot.json` has `active: false`.
While it is false, `pilot-cycle --live` refuses to start, and so does every `publish-*` command
given `--live`. Activation needs a reviewed change plus the storage decisions in "Before a live
start".

## What one cycle does

`python -m market_data.news pilot-cycle` runs once and exits; the scheduler runs it every 15 minutes.

1. If `news/v1/publish/stop.json` exists, it exits 0 with `status: stopped`. Nothing is collected or written.
2. It collects from the four enabled sources: OilPrice, EIA Today in Energy, TechCrunch and Ars
   Technica. The streams are `oil` and `ai_buildout`. It uses the accepted M2.1 archive, which
   provides the writer lease, spooling, the manifest commit, commit.json availability and
   per-entry failure isolation.
3. It builds sanitized views for the last 7 UTC dates with the accepted M4a builder. No price input
   is connected, so every price is `missing`, pages carry `price_series.input: "unavailable"`, and
   the data label says "prices unavailable".
4. It publishes the views to the public store, in this order (see `publish.py`):
   - The index and every page it names are checked before any write: no state, snapshot, lease,
     receipt or public object is touched by a generation that fails.
     - The index must carry every field the panel and publisher read (`data_label`, `build_as_of`,
       `complete`, `overflow_reasons`, `views_total`, `views_omitted`, and per view `mode`, `stream`,
       `date`, `first_page`, `page_count`, `rows_total`, `complete`). Its counts must agree:
       `views_total` = listed + omitted, and `complete` only when nothing is omitted or overflowed.
     - Each page must pass the panel's `validatePage` checks (kept in step by a test) and also have
       every nested shape the unchanged renderer reads: rows (instruments, price, time quality,
       classification, later context), coverage segments and their sources, price intervals, tail
       and hindsight lists (`PAGE_SPEC` in `publish.py`). A test runs the real renderer over every
       such field set to each wrong shape and requires the publisher to refuse every case the
       renderer cannot render cleanly.
     - Its mode, stream, date, page number, page count and `build_as_of` must match its key and
       index entry. Its `prev_page`/`next_page` must form exactly the chain page-0001 to page-N.
       Orphan pages are refused.
   - Pages are written next, highest page number first within each view, then `index.json` last.
     Stale pages are deleted only after that.
   - The generation id is the build time plus a SHA-256 over every key and its bytes, so snapshots
     and restores are byte-exact.

## Replacement and navigation (fixed keys)

Page keys are fixed (`.../page-NNNN.json`), so a page is visible to readers of the old index the
moment it is written. Writing pages from the highest number down means that, after any prefix of
the writes or a crash at any point, every page reachable from the live index exists and renders.
Tests check this by crashing at every write and following real `next_page` links, for growth,
shrinkage and same-size replacement.

**Known limit.** A view with two or more pages changes builds one page at a time. Until the commit
(or the next cycle after a failure), a reader paging through it can see pages from two builds. Each
page shows its own `build_as_of`. Views of one page switch in a single write. Removing this needs
generation-specific keys. That proposal is below and is not implemented, because it changes the
panel and proxy contract.

**Truncated index.** If the builder cuts the index (over `index_views`, 500, or the 256 KB
`index_bytes` cap), it retains whole views newest date first. Within a date it keeps the build order
(mode, then stream), so the first views dropped are the oldest date's last ones. The retained views
are still listed in build order, and the cut is deterministic. It emits pages only for the views it
still lists, so the generation has no orphan pages and publishes. The index says `complete: false`,
`views_total` counts every view built and `views_omitted` the rest, and `overflow_reasons` names the
cap. The panel shows "INCOMPLETE index: N of M views omitted". The publisher stays strict: an index
whose reasons are not the index-level ones, or that omits views without a reason (or gives a reason
with nothing omitted), is refused like any other invalid generation, and the last good publication
stays live. The byte cap is checked with the counters at their widest, so an index needs about
50 bytes of headroom under the cap to stay complete. A cut so small that no view fits (views built,
none listed) is refused before any private or public write, so the last good publication stays live
and the cycle fails visibly. This is separate from empty sources: an empty archive still builds
complete views with no rows, which publish, and an index with no views built (`views_total` 0)
remains valid. The pilot's 7 days × 9 views = 63 views is far below either cap.

## Storage

| Store | Holds | Readers |
|---|---|---|
| private | `news/v1/runs/**` (immutable raw history), `news/v1/index/**`, `news/v1/latest.json`, `news/v1/_writer/lease.json`, `news/v1/publish/**` (state, stop marker, publish lease, snapshots, receipts) | the collector/publisher only |
| public | `news/v1/views/index.json`, `news/v1/views/<mode>/<stream>/<date>/page-NNNN.json` | the site: same-origin `/browser-data/` proxy and direct public-R2 path |

The public store wrapper refuses every key except the exact view-key rule the panel and proxy use.
A test pins that the Python rule matches the panel's `isNewsViewKey`. The two stores must be
distinct buckets (or distinct non-nested directories locally), configured through separate
`NEWS_PRIVATE_R2_*` and `NEWS_PUBLIC_R2_*` variables.

## Cadence and load (estimates)

- **Schedule:** `*/15 * * * *` UTC, one-shot, restart policy NEVER. A cycle that finds a lease held
  exits non-zero without writing, and the next tick retries.
- **Fetch:** 4 requests per cycle, with the accepted 180 s budget and the 4-worker bound.
- **Public writes:** each cycle rewrites every page of the 7-day window, because `build_as_of`
  changes. That is 63 pages plus the index, about 64 PUTs and 64 GETs per cycle, about 6.1k PUTs a
  day, plus deletions when a date leaves the window. Identical bytes are skipped (restore,
  re-publish). Page and index sizes stay within the accepted 256 KB caps.
- **Private reads (history checkpoint).** This reduces object GETs; it does not make a cycle's work
  constant. Without the checkpoint, every cycle verified all committed
  runs three times (collect, the index rebuild and loading for views). That is 4 objects per run, so
  about 1.2k GETs per cycle at 96 runs, rising to about 8k by day 7. With the checkpoint (see
  `README.md`, "History checkpoint"), a cycle makes a constant 26 private GETs in the fixture
  measurement, plus 4 per run it has not seen yet. Each history read is one listing plus the head
  and body. Once a day the writer replays everything (`FULL_VERIFY_S`), which costs one full-replay
  cycle. The body's bytes still grow with history, because it holds every record. It is about the
  size of the compressed archive (the raw-archive estimate below is about 1 MB a day, so about 7 MB
  at day 7; unmeasured at live item counts), and it is read about 3 times and written once per
  cycle, so bytes transferred and the memory to hold it grow with history. The run listing also
  grows by 4 keys a run, and R2 returns at most 1,000 keys per list page, so list requests grow too
  (about one more per 250 runs, roughly every 2.6 days). Measured counts are in
  `tests/test_news_checkpoint.py` (`ReadCounts`); they are local fixture counts, not a validated
  capacity limit.

## Retention

- **Raw archive:** kept, immutable. It is the research record. Size is roughly 1 MB a day at this
  source count (estimate). Review at the end of the pilot.
- **Public views:** the last 7 UTC dates. Older pages are deleted after each new index is committed.
- **Snapshots:** the last 3 published generations, in the private store. The live generation and its
  predecessor are never pruned.
- **Receipts:** one per publish, restore and withdrawal, kept for the pilot.

## Ownership

| Role | Who | What |
|---|---|---|
| Activation, buckets, credentials, scheduler service | Owen | provider changes; approves the config flip |
| Code, operating checks, incident first look | Jayden | reads `publish-status`, runs stop/restore/withdraw when asked |
| Review | Chej | review before activation and after the first live day |

## Failure visibility

`python -m market_data.news publish-status` returns JSON. It exits 1 when any of these problems is
present:

- `last_publication_failed`: the stage (pages, index, cleanup, state) and the error are in `last_failure`.
- `publication_interrupted`: a generation was started and not committed.
- `published_views_stale`: the last publication is older than 2 × 15 min + 180 s and the pilot is not stopped.
- `public_index_differs_from_state`: the live index is not the one the publisher committed.
- `never_published`: no generation has ever been committed, and the pilot is neither stopped nor withdrawn.
- `stale_writer_lease` / `stale_publish_lease`: an expired lease left by a process that did not exit
  cleanly. The `leases` field shows the holder and expiry. An unexpired lease is listed but is not a
  problem by itself, because a cycle may be running.

It also reports leftover non-view keys under the public prefix, the stop marker and retained snapshots.

A generation refused by validation writes nothing, not even `last_failure`, so the old publication
and the publisher state stay byte-identical. The cycle exits non-zero with the reason (for example
`page.rows[0].instruments is not an object`), and `publish-status` reports `published_views_stale`
once the last publication is older than the threshold above.

Readers can see failures too. Every page shows `build_as_of`, and collection failures are part of
the published coverage: failed sources, missed intervals and partial runs (accepted M3/M4a semantics).

A failed publication never leaves the live index pointing at a missing page. Pages written before
the failure may already hold the newer build of the same key, and each page carries its own
`build_as_of`. The next cycle republishes everything and removes leftovers.

## Stop, restore, roll back

| Action | Command | Effect |
|---|---|---|
| Stop | `publish-stop --reason "..."` | Cycles no-op (exit 0). Views stay as last published and age visibly. |
| Resume | `publish-resume` | The next tick collects and publishes normally. |
| Roll back views | `publish-stop`, then `publish-restore --gen <gen_id>` | Republishes a retained snapshot byte-for-byte through the same safe order. It stays in place until resume. |
| Take views down | `publish-stop`, then `publish-withdraw` | Deletes `index.json` first, so the panel shows "No news views published yet", then every page. Snapshots are kept. |
| Code rollback | redeploy the previous revision | The raw archive is append-only and versioned. Views are rebuilt on the next cycle. |

`gen_id` values are listed in `publish-status` (`snapshots`) and in receipts.

### Recovering from an abandoned lease

A cycle or publisher that is killed (OOM, redeploy, host restart) leaves its lease behind. Every
later cycle then exits non-zero with "writer lease held by …", and `publish-status` reports
`publication_interrupted` or `never_published`. After the lease expires (15 minutes), it also
reports `stale_writer_lease` or `stale_publish_lease`. Leases are never taken over automatically.

1. Run `publish-status` and read `leases`: note the holder (host:pid) and `expires_at`.
2. Confirm that holder is not running: no scheduler execution in progress, and no local process.
3. After `expires_at`, run one cycle with `pilot-cycle --break-stale-lease`. The same flag exists
   on `publish-restore` and `publish-withdraw`. An unexpired lease is still refused.
4. Run `publish-status` again. It should be healthy, with `recovered_from` in the cycle output and
   receipt. The writer side also recovers any spooled run, reusing its run id.

## Before a live start (decisions needed)

1. **Private bucket.** The raw archive must not live in a publicly readable bucket. The public R2
   origin the site reads (`pub-…r2.dev`) appears to expose the whole bucket it fronts. That is an
   assumption to verify, but if it holds, `news/v1/runs/**` must go elsewhere. This needs a new
   private bucket plus credentials.
2. **Public bucket and credentials.** Use the bucket behind `/browser-data/` and the public origin.
   The credentials should be scoped to `news/v1/views/` if the provider supports prefix-scoped
   tokens. Otherwise they are bucket-wide, and the code-level key guard is the only fence.
3. **Scheduler service.** A new Railway cron service, or other runner, with the cron above. Its
   watch paths must not overlap the collectors' (`market_data/news/**` is not watched by them today;
   see #225). Its variables are `NEWS_ALLOW_BUCKET_WRITES=1` plus the two prefixed R2 sets.
   `NEWS_GIT_REVISION` is optional.
4. **Pilot length.** Up to 7 days, as before. The history checkpoint cuts the per-cycle object GETs
   (see "Cadence and load"), but checkpoint bytes and memory, the run listing and R2 list pagination
   still grow with history, and none of this is measured at live volumes. Any longer pilot is a
   separate decision.
5. **Release of the site side.** M4b (panel, proxy keys) must be released first, or the views are
   published but not shown. That release is separate and governed by #225.

## Proposal (not implemented): generation-specific page keys

This would make a multi-page view switch builds atomically.

- **Keys:** pages move to `news/v1/views/g/<gen_id>/<mode>/<stream>/<date>/page-NNNN.json`.
  `<gen_id>` is exactly `YYYYMMDDTHHMMSSZ-<16 hex>`. `index.json` stays at its fixed key and is still
  the only commit point; its `first_page` values, and every page's `prev_page`/`next_page`, name keys
  of the same generation.
- **Publication:** write the whole new generation (never visible, since nothing links to it), then
  `index.json`. Delete earlier generations only after a grace period (proposed: 2 cycles), so a
  reader who loaded the old index can finish paging. Each page key is then written once and never
  changes, which also allows long cache lifetimes.
- **Shared changes this needs, for review before any edit:**
  - `site/js/news-panel.js` `isNewsViewKey`: accept the generation segment and require every
    followed link to stay in the generation named by the index.
  - `backend/browser-data.js` `isPublicNewsViewKey`: the same exact pattern.
  - Their tests.
  - The asset gateway's copied allowlist (#225, Musef), if that lands first.
- **Cost:** 63 new page objects per cycle instead of rewrites in place, plus deletions of expired
  generations. Write counts are about the same; storage is about 3 generations' worth at once.
