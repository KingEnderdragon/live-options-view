# Shared producer image

The live collector, Tradier archive collector and OA-203 scanner share this Python dependency set and Docker image recipe. Build from the repository root:

```sh
docker build -f runtime/python/Dockerfile .
```

Service launch commands and provider settings are documented in:

- [Live collector](../../collector/README.md): `python -m collector`.
- [Tradier archive](../../collectors/tradier/OPERATIONS.md): `python -u -m collectors.tradier.launcher`.
- [OA-203 scanner](../../scanners/oa203/README.md): `python -u scripts/oa203_return_scanner.py run`.

All three use repository-root build context and `runtime/python/Dockerfile`.
Railway owns their build/start/watch settings and schedules; no `railway.toml`
is loaded for these services. Railway rejected new legacy config-file assignments
during the November 8 cleanup, so this move uses supported service settings.
Nixpacks is not used by these Docker builds. This directory owns packaging,
not trading or collection behavior. Crassus and Market MCP have separate images.
