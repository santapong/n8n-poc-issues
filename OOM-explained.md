# How the n8n web node died (and how to fix it)

A picture-first explanation of the `JavaScript heap out of memory` crash we
reproduced on the **main (web) node** in queue mode, using the real numbers
measured from `/metrics` and a V8 heap snapshot.

---

## 1. The death sequence

```
   8 concurrent  ×  200 MB uploads   (≈ 1.6 GB of bytes)
            │
            ▼
   ┌────────────────────┐
   │       nginx        │   client_max_body_size 0
   │   (front door)     │   proxy_request_buffering off
   │                    │   → STREAMS straight through, absorbs nothing
   └─────────┬──────────┘
             │
             ▼
   ┌─────────────────────────────────────────────────────────┐
   │            n8n-main  ·  THE WEB NODE                      │
   │            V8 heap cap = 512 MB (--max-old-space-size)    │
   │                                                           │
   │   step 1  receive multipart  ─▶  Buffer      (OFF-heap)   │
   │   step 2  base64-encode file ─▶  STRING      (ON-heap) ×1.37
   │   step 3  JSON.stringify exec ─▶ more strings (ON-heap)   │
   │   step 4  push execution to Redis queue                   │
   │                                                           │
   │   steps 2–3 pile hundreds of MB of STRINGS onto the heap  │
   └─────────────────────────────────────────────────────────┘
             │
             ▼   heap crosses 512 MB during step 2/3
      💥  FATAL ERROR: Reached heap limit — JavaScript heap out of memory
          process aborts (exit 134)   ← NOT a kernel kill (OOMKilled=false)
```

The nginx settings are the trap: it does **not** buffer the upload to disk, so
all the pressure lands in the web node's heap.

---

## 2. The heap filling up (real measured numbers)

`heapUsed` climbing toward the 512 MB cap, from the live `/metrics` samples:

```
 heapUsed          0        128       256       384      512 (CAP)
                   ├─────────┼─────────┼─────────┼─────────┤
 idle      145 MB  ███████████                                  ok
 uploading 222 MB  █████████████████                            climbing
 serialize 503 MB  ██████████████████████████████████████░  💥 FATAL
                                                           ▲
                                            V8 aborts here (exit 134)
```

---

## 3. WHERE the memory lives — this is the key insight

Two different memory regions. `--max-old-space-size` only caps ONE of them.

```
            V8 HEAP  (capped at 512 MB)              OFF-HEAP (external)
        ┌──────────────────────────────┐        ┌───────────────────────┐
 base64 │ ████████████████████ 325 MB   │        │  Buffer  ~117 MB      │
 strings│                              │        │  (raw uploaded bytes)  │
 exec   │ ████████ ~100 MB              │        │                       │
 JSON   │                              │        │                       │
        └──────────────────────────────┘        └───────────────────────┘
                    ▲                                       ▲
        --max-old-space-size caps THIS.          NOT limited by
        The base64 TEXT of your file lives        --max-old-space-size.
        here → this is what hits 512 → OOM.       Raw Buffer sits here.
```

From the heap snapshot taken at 334 MB heap: **77% of the heap was `string`**
(325 MB). That string mass *is* your uploaded file, base64-encoded, plus the
execution JSON. The raw file Buffer is off-heap and is NOT the thing that
overflowed — its base64/JSON representation is.

> In queue mode it's worse: that base64 execution data is pushed through Redis
> to the **worker**, which base64-*decodes* it into its own heap. Both nodes pay.

---

## 4. Why `default` mode (binary inline in the DB) does NOT help

`N8N_DEFAULT_BINARY_DATA_MODE=default` already stores the file in Postgres — but
only *after* base64-encoding it on the heap. The DB is the destination; the heap
is the toll booth every byte passes through.

```
 upload ─▶ Buffer(external) ─▶ base64 STRING(heap) ─▶ JSON ─▶ Postgres
                                     ▲
                             the OOM happens HERE,
                             before the DB write.
```

The `filesystem`, `s3`, and `database` modes instead keep binary as a *reference*
(no base64-in-heap). `filesystem` is out on Cloud Run (tmpfs = RAM, no shared/persistent
disk); `s3`/`database` work but change infra. Note: binary modes only move **binary
files** — they don't help the JSON/array-item OOMs (that's the worker story).

---

## 5. How to fix it

### The governing formula

```
   heap needed  ≈  concurrency  ×  max_upload_MB  ×  2.3
                   └──── bound BOTH of these, or raise the heap ────┘
```

### Fix A — bound the inputs (no infra change) ✅ fits Cloud Run

```
   uploads ─▶ nginx: client_max_body_size 25m ──▶ 413 Too Large if bigger
                  +  Cloud Run --concurrency = 2
                          │
                          ▼  at most 2 × 25 MB in flight
              n8n-main heap:  2 × 25 × 2.3 ≈ 115 MB   ✓ safe under 512
```
Settings:
- nginx: `client_max_body_size 25m;`
- n8n:   `N8N_PAYLOAD_SIZE_MAX=25`, `N8N_FORMDATA_FILE_SIZE_MAX=25`
- Cloud Run: `--concurrency=2` (caps simultaneous requests per instance)
- size the instance heap for the formula above

### Fix B — keep the bytes OUT of n8n (best for large files)

```
   client ───upload the file───▶  GCS bucket (object storage)
      │                              ▲
      └── sends n8n only the ────────┘   n8n receives a URL/reference (KB),
          object URL / reference           never the file → heap stays flat,
                                           Postgres stays small
```
Use a signed upload URL; pass the resulting object path into the workflow.
App change, but it removes the heap AND the DB-bloat problem entirely.

### Fix C — external binary mode (parked)

`s3` mode works and is the "official" answer for scaled n8n; on GCP that's a
GCS S3-compatible bucket. You're avoiding this because it changes infra.
`filesystem` mode is out on Cloud Run (no shared/persistent disk).

---

## Crash signature cheat-sheet

| You see | Meaning |
|---|---|
| `FATAL ERROR: ... JavaScript heap out of memory`, exit **134**, `OOMKilled=false` | V8 hit `--max-old-space-size` first (what we reproduced) |
| container killed, exit **137**, `OOMKilled=true` | kernel/Cloud-Run memory limit hit first (heap cap ≥ container memory) |

---

*Last synced: 2026-07-02 · config reverted to production shape · stack stopped.*
