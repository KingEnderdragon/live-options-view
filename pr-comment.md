## CI at f3a9e78 — green, including both image builds

- **Collector suite** (`verify-collector`, 51s): stage2 13/13, auth safety, Tradier probe, Tradier collector, OA-203 scanner, OA-203 active, cross-market overview, **new price observation provenance suite**, symbol probe safety, OA-203 windows, capture diagnostics, VWAP/RVOL, SPY isolation, crude, EIA STEO, weekly — plus `docker build -f deployment/python/Dockerfile` and `producer_packaging.py` inside that image.
- **Market MCP consumer** (34s): 151 Node tests, squeeze display test, fixture smoke, authenticated production smoke, 256 MB load run, `docker build -f market_mcp/Dockerfile`, and `verify-image.sh` as non-root with offline authenticated fixtures.
- **Web CI** (37s): unaffected, passing.

Both Docker checks I could not run locally passed in CI.

## Cross-boundary check (local, synthetic state)

I also ran the real producer code path against the real consumer rather than only each side's fixtures: built an 18-row schema-v2 payload through `push_prices` with a stub sink (DXLink event rows, a SPY midpoint, an unresolved `/6J`, a dated Yahoo VIX bar, and an unrelated empty state), then called `overview.validate` and `overview.rows` on it.

- `validate` accepted the payload; `rows` returned 100 rows with no unmapped labels.
- AAPL kept its provider event time and reported `change.status = partial` with the honest reasons (`adjustment_basis_unverified`, `reference_precision_or_timezone_unverified`, `observation_and_reference_session_unverified`) rather than `ok`.
- SPY's midpoint kept its observed time but no session, so no live claim.
- Every row was `stale`/not live because the synthetic events were ten days older than the artifact cycle, which is the intended "old data fetched now stays old" behavior.
- A VIX pair whose reference bar post-dated the current bar was withheld as `invalid_reference_date`, which was my deliberately contradictory fixture, not a code path I want to keep.

This is fixture-level integration evidence. It is not provider, deployment or connected-client evidence, and I am not claiming any of those.
