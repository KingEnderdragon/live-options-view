# Deployed quote-panel acceptance — 2026-11-09

**Deployed artifact.** `https://live-options-view.otmooper12.workers.dev/` served
HTTP 200 and build `e5efd57-20261109074412` in the page's stamped `BUILD_ID`.
Merge `e5efd579b5e1f42a9931676bbe781f1ed369216c`; Cloudflare deployment run
`42581103951` succeeded. HTTP/build readback is not rendered acceptance; the
observations below come from an actual Edge window on Snapdragon.

**Device and control.** Snapdragon `86ac94d6-7f2f-4a94-a0aa-71746b1705d3`,
Windows 11 ARM64, guide
`C:\Users\MooperTest\Documents\Codex\SnapdragonRemote\SNAPDRAGON-REMOTE-GUIDE.md`
read first. Foreground was the muse.ai Nova Edge conversation, so I opened one
new Edge window on the existing profile, operated only that window, then closed
it and restored Nova's window as foreground (`GetForegroundWindow` returned
Nova's handle `17238634`). Brian's running harness was not touched. No order,
trade, login, logout, account setting or other user window was changed.

**Viewport.** Initial window 1280x900 physical. Narrow run was 772x900 physical;
the device's 150% scale makes the page viewport roughly 515 CSS pixels, below
the 640px breakpoint, so the narrow layout was exercised. This is a narrow
desktop window, not a mobile device emulation.

## Observed sequence

1. Loaded the deployed page in the new window. Core rendered 8 cards; pins were
   the existing four in order `SMH, IGV, USO, AAPL` (`4/12`); movers showed
   `US equities · 0 eligible of our tracked universe`. Screenshot:
   `desktop-live.png`.
2. Narrowed the window to 772px. Core wrapped, the panel remained usable and no
   horizontal scrollbar appeared. Screenshot: `narrow-772px.png`.
3. Opened `+ Pin instrument`, searched `NFLX`, and invoked `Pin NFLX`. The count
   changed to `5/12` and the fifth card appeared on the next row. Screenshot:
   `pins-five-after-refresh.png`.
4. Invoked `Move NFLX left` and verified the order moved to
   `SMH, IGV, USO, NFLX, AAPL`; invoked `Move NFLX right` and restored
   `SMH, IGV, USO, AAPL, NFLX`.
5. Reloaded the page. `NFLX` was still present and still last; count remained
   `5/12`. Screenshot: `pins-persisted-after-reload.png`.
6. Invoked `Unpin NFLX`, restoring the original four cards and `4/12`.
   Screenshot: `pins-restored-four.png`.
7. Focused `Move SMH right`, waited through one 15-second controller refresh,
   and checked the same UIA control: `has_keyboard_focus` was true before and
   after, with the same runtime id `(42, 12847688, 4, 628, 8, 15600)`.
   Screenshot: `focus-retained-after-refresh.png` shows the focus ring on the
   same control.
8. Opened the `Automated` tab; the Automated Accounts panel rendered, with no
   order entry, trade or account action. Returned to `Live`. Screenshot:
   `automated-tab.png`.

## Findings

- **Confirmed defect: stale rows claim an open extended session.** With the old
  producer (no `price_lane` metadata), all rows were stale (QQQ 59h, most others
  3h) and it was Sunday night in Pacific / early Monday ET, yet the session
  banner read `US equities · extended hours`. Movers correctly showed zero
  eligible rows. The label came from the rows' last trade sessions
  (`regular`/`extended`), not from current session evidence.
  - Follow-up fix on this branch: without a producer lane, only rows whose
    state is `live` may name the active group. If no fresh row supports the
    clock fallback group, the label is `Session unknown`. A targeted model and
    DOM regression reproduces the deployed stale case.
- **Observed, not a UI defect:** QQQ and the four pins showed `STALE` with
  `updated 59h ago`; SPY showed `UNKNOWN`; futures and daily-source cards showed
  `DAILY`. This is the old producer artifact, not extended-session coverage.
- **Not verified:** premarket, after-hours, overnight-futures or weekend-crypto
  automatic publication; live movers; reconnect/restart recovery. The producer
  remains the old one and this run must not be used to close G4 or #179's
  real-session criteria.

## Evidence files

All images are unedited captures of the controlled Snapdragon window:
`desktop-live.png`, `narrow-772px.png`, `pins-five-after-refresh.png`,
`pins-persisted-after-reload.png`, `pins-restored-four.png`,
`focus-retained-after-refresh.png`, `automated-tab.png`.
