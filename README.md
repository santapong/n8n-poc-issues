# n8n queue-mode OOM lab

An n8n stack in **queue mode**, behind **nginx**, with **Prometheus `/metrics`**
enabled, and both Node processes capped at **512 MB heap** — built to reproduce a
web (main) node crashing with *"JavaScript heap out of memory"* during large file
uploads.

## Architecture

```
  you ──:8080──▶ nginx ──▶ n8n-main:5678   (web node · /metrics · 512MB heap)
                            │        │
                         Postgres  Redis ──▶ n8n-worker   (512MB heap)
   Prometheus:9090 ─┐
   Grafana:3000  ───┴─ scrape n8n-main:5678/metrics   (--profile monitoring)
```

| Service | Port | Notes |
|---|---|---|
| nginx | `8080` | front door → n8n-main |
| n8n-main | (internal 5678) | UI/API/webhooks + `/metrics`, capped at 512MB heap |
| n8n-worker | — | executes queued jobs, capped at 512MB heap |
| postgres / redis | — | database + Bull queue |
| prometheus | `9090` | optional, `--profile monitoring` |
| grafana | `3000` | optional, `--profile monitoring` (admin/admin) |

## Prerequisites

- Docker + Docker Compose v2
- `.env` present (already created from `.env.example`) — **change `N8N_ENCRYPTION_KEY`** before any real use.

## Run

```bash
# core stack
docker compose up -d

# with Prometheus + Grafana
docker compose --profile monitoring up -d

docker compose logs -f n8n-main        # watch the web node
```

Open the editor at **http://localhost:8080**.

Confirm queue mode + metrics:

```bash
curl -s http://localhost:8080/metrics | grep -E "nodejs_heap_size_used_bytes|n8n_"
```

## Reproduce the OOM

1. **Import the workflow**: n8n UI → *Workflows → Import from File* →
   `workflows/upload-oom.json`, then **toggle it Active**. This publishes
   `POST http://localhost:8080/webhook/upload`.

2. **Flood it with large uploads** (Git Bash / WSL / macOS / Linux):

   ```bash
   ./scripts/upload-oom.sh
   # tune it:
   SIZE_MB=250 CONCURRENCY=10 ROUNDS=100 ./scripts/upload-oom.sh
   ```

   PowerShell alternative (single large upload, repeat/parallelize as needed):

   ```powershell
   $f = ".oom-payload.bin"
   fsutil file createnew $f 209715200   # 200MB
   1..8 | ForEach-Object {
     Start-Job { curl.exe -s -o NUL -F "file=@$using:f" http://localhost:8080/webhook/upload }
   } | Wait-Job | Receive-Job
   ```

3. **Watch it die.** In `docker compose logs -f n8n-main` you'll see:

   ```
   FATAL ERROR: ... JavaScript heap out of memory
   ```

   and the container restart (auto-restart is on). To observe the crash without
   auto-restart masking it, set `restart: "no"` on `n8n-main` (commented hint in
   `docker-compose.yml`) and `docker compose up n8n-main`.

## Why it OOMs (the knobs)

| Setting | Effect |
|---|---|
| `NODE_OPTIONS=--max-old-space-size=512` | V8 heap capped at 512MB on **both** nodes |
| `mem_limit` (1024m) > heap cap | V8 throws the heap error *before* a kernel OOM-kill — matches the real symptom |
| `N8N_DEFAULT_BINARY_DATA_MODE=default` | uploaded files kept **in memory**, not spilled to disk → they eat heap |
| `N8N_PAYLOAD_SIZE_MAX=256` + nginx `client_max_body_size 0`, `proxy_request_buffering off` | large uploads stream straight into the main node's heap |

## Things to try next

- Flip `N8N_DEFAULT_BINARY_DATA_MODE=filesystem` → uploads spill to disk, heap
  stays flat, no OOM. Good A/B contrast.
- Raise `NODE_MAX_OLD_SPACE_SIZE` in `.env` and re-run to find the survival point.
- In Grafana (http://localhost:3000), graph `nodejs_heap_size_used_bytes` to see
  the sawtooth climb to ~512MB right before each crash.

## Reset

```bash
docker compose down -v      # also wipes postgres/n8n/grafana volumes
rm -f .oom-payload.bin
```
