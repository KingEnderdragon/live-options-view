// countdown timer — extracted from docs/index.html (B2, readability quest,
// Nov 2026). Classic script, loaded by index.html in the same position the
// inline section occupied, so the page's script order — and the shared global
// scope — is unchanged. No ESM conversion.
// ── countdown timer ───────────────────────────────────────────────────────────
// Drives the 60s refresh cycle: every second the countdown ticks down, and
// at zero it re-fetches the latest chain (`fetchLatest`, data fetch cycle)
// and resets. `countdown`, `timerHandle`, and `REFRESH` live in the page's
// shared scope (declared just above the data fetch cycle); this module only
// owns the timer itself. Called from `selectUnderlying` (re-arm on symbol
// switch) and from boot.
function startCountdown() {
  countdown = REFRESH;
  clearInterval(timerHandle);
  timerHandle = setInterval(() => {
    countdown--;
    if (countdown <= 0) {
      fetchLatest();
      countdown = REFRESH;
    }
  }, 1000);
}
