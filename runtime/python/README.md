# Shared producer image

The live collector, Tradier archive collector and OA-203 scanner share this Python dependency set and Docker image recipe. Build from the repository root:

```sh
docker build -f runtime/python/Dockerfile .
```

Service launch commands belong to `collector/railway.toml`, `collectors/tradier/railway.toml` and `scanners/oa203/railway.toml`. Their cron schedules and activation policies remain provider-managed. This directory owns packaging, not trading or collection behavior.
