# Shared producer image

The live collector, Tradier archive collector and OA-203 scanner share this Python dependency set and Docker image recipe. Build from the repository root:

```sh
docker build -f runtime/python/Dockerfile .
```

Service launch commands and provider settings are documented in:

- [Live collector](../../producers/live_market/README.md): `python -m producers.live_market`.
- [Tradier archive](../../producers/tradier/OPERATIONS.md): `python -u -m producers.tradier.launcher`.
- [OA-203 scanner](../../producers/options_scanner/README.md): `python -u -m producers.options_scanner run`.

All three use repository-root build context and `runtime/python/Dockerfile`.
Railway owns their build/start/watch settings and schedules; no `railway.toml`
is loaded for these services. Railway rejected new legacy config-file assignments
during the November 8 cleanup, so this move uses supported service settings.
Nixpacks is not used by these Docker builds. This directory owns packaging,
not trading or collection behavior. Crassus and Market MCP have separate images.
