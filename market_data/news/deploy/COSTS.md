# News pilot: runtime, operations, storage and cost envelope

Status: evidence for sizing the schedule within Owen's **$5/month incremental** News budget
(2026-11-14). Nothing here is provisioned. The pilot config stays `active: false`.

Each figure is labelled:
- **OBSERVED:** measured from the shipped code on local stores;
- **PROJECTED:** arithmetic on observed figures plus a stated assumption;
- **NEEDS PROVIDER ACCESS:** cannot be measured without the provisioned buckets or service.

## How it was measured

- `tests/news_cycle_cost_measure.py` runs `publish.pilot_cycle` with the shipped config, once per
  simulated 15-minute tick, against two counting local stores. Every store call is recorded as the R2
  request it maps to in `R2ObjectStore`:
  - put → PutObject (Class A);
  - list → ListObjectsV2 (Class A, one request per 1,000 keys);
  - get → GetObject (Class B);
  - `last_modified` → HeadObject (Class B);
  - delete → DeleteObject (free).
- `--expiry` simulates a verified store-clock bound (`clock_bound_verified: true`) in a temporary copy
  of the config. The shipped value stays `false`.
- `tests/news_cost_projection.py` turns a measurement file into monthly figures. Its assumptions are
  listed in its docstring and repeated below.
- Runs behind this document:
  - 96 cycles, expiry off;
  - 192 cycles, expiry on;
  - 1 cycle against the four live public feeds (reads only, local stores).

## Observed

| Measure | Expiry off (96 cycles) | Expiry on (192 cycles) |
| --- | --- | --- |
| Publications | 96/96 published | 192/192 published |
| Class A per cycle (private + public) | 168 → 193 (public listings grow) | 170 (flat) |
| Class B per cycle | 169 → 229 | 231 (flat) |
| Public pages written per cycle | 64 | 64 |
| Public objects held at the end | 6,049 (still growing) | 316 (flat from cycle 5) |
| Old public pages deleted per cycle | 0 | 63 (free) |
| Private growth per cycle with no new items | ~60 KB, 6 objects | ~58 KB, 6 objects |

- **Expiry off means publication pauses after about a day.** Nothing expires, so the public page
  count passes 6,000 after 96 cycles. The 6,300 cap is reached at about cycle 100 (~25 h at
  `*/15`), and then publication pauses. Continuous operation needs the clock-bound check passed and
  `clock_bound_verified: true` first, or pauses must be accepted.
- **The raw archive is listed in full four times every cycle.** `news/v1/runs/` grows by exactly 4
  objects per cycle (measured over 12 cycles) and is listed four times per cycle. Raw runs are kept
  ("keep (immutable history)"), so those listings grow by 4 ListObjectsV2 requests per 1,000 runs:
  about +46 Class A per cycle per month at `*/15`. This is the only term that grows without bound.
- **Live feeds** (2026-11-14, a Saturday):
  - All 4 sources were OK, with 82 items on first sight.
  - The first cycle's private store was 1.04 MB, including one view snapshot.
  - Feed fetch plus cycle took about 2 s of local wall time.
  - The feeds' own publication rates add up to about 35 items/day: ars_technica 10.3, oilprice_com
    13.0, techcrunch 10.8, eia_today_in_energy 0.4.
- **Process:** one fixture cycle through the launcher on Linux (WSL) took 0.45 s elapsed with a
  peak RSS of about 29 MB.
- **Local wall time is not a runtime measurement.** Local time rose from 1.3 s to 10.3 s over 192
  cycles. A profile shows that is the local store's directory walk (`rglob` over directories left
  empty by deletes), which R2 does not have. R2 listing cost is counted above as requests instead.

## Projected

Assumptions (in `tests/news_cost_projection.py`):
- **Prices** (list prices checked 2026-11-14):
  - R2 Standard: Class A $4.50/M, Class B $0.36/M, storage $0.015/GB-month; deletes and egress free.
  - Railway: $0.000463 per vCPU-minute, $0.000231 per GB-RAM-minute.
- **Free tier not subtracted.** The R2 free tier (1 M Class A, 10 M Class B, 10 GB) is account-wide
  and already shared with the market collectors.
- **Failure headroom:** ×1.25 on every request (failed or retried cycles; a 412 is billed as Class A).
- **Runtime:** about 95 s per cycle. That is 0.15 s per R2 request, serial, plus 10 s for feeds. The
  latency is **assumed**; only the request count is measured.
- **Compute:** 0.5 vCPU and 0.1 GB while running (about 29 MB was measured).
- **Archive growth:** 35 items/day at 4 KB each in the private store.

| Month | `*/15` Class A | `*/15` total | `*/30` Class A | `*/30` total |
| --- | --- | --- | --- | --- |
| 1 | 688 k | **$4.56** | 323 k | **$2.19** |
| 2 | 853 k | $5.31 | 365 k | $2.38 |
| 3 | 1.02 M | $6.06 | 406 k | $2.56 |
| 6 | 1.52 M | $8.31 | 531 k | $3.13 |
| 12 | 2.51 M | $12.80 | 779 k | $4.26 |

Breakdown at `*/15`, month 1:
- R2 $3.40, of which Class A ≈ $3.10.
- Railway $1.17.
- Storage is negligible: about 0.18 GB after a month, about 2 GB after a year.

**Reading:**
- `*/15` is not within $5 with headroom. It projects $4.56 in month 1 and passes $5 in month 2,
  rising about $0.75/month from the raw-archive listings.
- `*/30` projects $2.19 in month 1 and $4.26 in month 12. It stays within $5 for the first year, with
  the most headroom early on.
- **Either cadence keeps growing.** The full listing of `news/v1/runs/` grows without bound. A
  bounded listing (only runs after the checkpoint) or a raw-archive retention decision would make
  Class A flat. That would be about $4.16/month total at `*/15`, still close to the limit, and about
  $2.1/month at `*/30`. That is a separate, reviewed code change. It is **not** in this PR.
- **The cadence is pinned in code.** `load_pilot_config` accepts only `*/15` with `interval_s` 900.
  `*/30` needs a reviewed config and validator change, with the reader timings rechecked.

## Retention cleanup (included above)

- Snapshots are capped at 3 (`retention.snapshots`). Older snapshot pages are deleted each cycle:
  68 deletes per cycle in the private store, free.
- Retired public incarnations are deleted after the reader grace once expiry is enabled: 63 per
  cycle, free.
- Raw runs, commit records and receipts are kept. They are the private growth above.

## Needs provider access

1. **R2 request latency from the scheduler host**, for list, get, put and conditional put on both
   News buckets. This decides the real runtime (projected about 95 s, well inside the 600 s
   deadline) and the Railway figure. It can be measured with the A1–A5 probes on disposable keys.
2. **Current account R2 usage against the free tier.** If allowance is left, the incremental R2 cost
   is lower than shown.
3. **Railway cron metering for a one-shot service.** Is it per running minute, with any minimum, and
   how long is the grace between SIGTERM and SIGKILL when a run is stopped? The launcher interrupts on
   SIGTERM and finishes within its interrupt grace (90 s). A shorter platform grace would turn a stop
   into a kill.
4. **The store-clock bound** (`clock_bound_verified`). Without it, publication pauses at the page cap
   after about 25 h at `*/15`.
