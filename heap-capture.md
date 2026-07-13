# n8n heap capture — raw contents

*Captured live from `n8n-main` via the heap monitor (`/api/heapraw`), n8n 2.28.6.*

Two snapshots of the **same web-node process**: once **idle**, and once **during a 120 MB PDF upload** (`max.pdf`, timed capture 2 s after firing). This shows *what is actually inside the V8 heap* — not just the number.

---

## Side-by-side summary

| Metric | Idle | During PDF upload | Δ |
|---|---|---|---|
| snapshot size on disk | 140.5 MB | 140.6 MB | — |
| objects in heap | 1,431,725 | 1,431,349 | — |
| **`string` bytes** | **64.3 MB** | **384.3 MB** | **+320.0 MB** |
| `native` (off-heap buffers) | 17.0 MB | 17.0 MB | +0.0 MB |
| **PDF blobs resident** | 0 | **1 (160.0 MB)** | — |

The idle heap is ~150 MB of engine + module source. During the upload the `string` region jumps from **64.3 MB → 384.3 MB** — that entire increase **is the uploaded PDF**, base64-encoded on the heap (plus a working copy), and `native` grows by the raw request buffer.

---

## Idle heap — by type

| type | count | size | share | |
|---|---|---|---|---|
| `string` | 269,359 | 64.3 MB | 39% | █████████ |
| `code` | 295,703 | 23.2 MB | 14% | ███ |
| `native` | 401 | 17.0 MB | 10% | ██ |
| `array` | 52,715 | 16.3 MB | 10% | ██ |
| `closure` | 299,691 | 15.2 MB | 9% | ██ |
| `object` | 203,846 | 11.4 MB | 7% | █ |
| `object shape` | 96,853 | 8.2 MB | 5% | █ |
| `concatenated string` | 103,281 | 3.2 MB | 2% |  |
| `hidden` | 52,709 | 2.2 MB | 1% |  |
| `sliced string` | 50,429 | 1.5 MB | 1% |  |

**Biggest resident strings (idle):** module source code, no upload data —

- `1.4 MB` — // AUTO-GENERATED — do not edit\n// Pre-compiled ajv validator for the OpenTelem
- `1.0 MB` — /**\n * @license\n * Lodash <https://lodash.com/>\n * Copyright OpenJS Foundatio
- `0.7 MB` — "use strict";\nvar __defProp = Object.defineProperty;\nvar __getOwnPropNames = O
- `0.5 MB` — 'use strict';\n\nObject.defineProperty(exports, '__esModule', { value: true });\
- `0.5 MB` — /*eslint-disable block-scoped-var, id-length, no-control-regex, no-magic-numbers

---

## During PDF upload — by type

| type | count | size | share | |
|---|---|---|---|---|
| `string` | 269,291 | 384.3 MB | 80% | ████████████████████ |
| `code` | 294,028 | 23.1 MB | 5% | █ |
| `native` | 404 | 17.0 MB | 4% | █ |
| `array` | 52,791 | 16.3 MB | 3% |  |
| `closure` | 299,852 | 15.2 MB | 3% |  |
| `object` | 204,229 | 11.5 MB | 2% |  |
| `object shape` | 97,474 | 8.3 MB | 2% |  |
| `concatenated string` | 103,291 | 3.2 MB | 1% |  |
| `hidden` | 52,806 | 2.2 MB | 0% |  |
| `sliced string` | 50,433 | 1.5 MB | 0% |  |

### PDF-only view (the filter)

The monitor's **"PDF upload only"** filter isolates the one string that matters:

```
160.0 MB   JVBERi0xLjQKJeLjz9MKMSAwIG9iajw8L1R5cGUvQ2F0YWxvZy9QYWdlcyAyIDAgUj4+CmVuZG9i
```

That string **is the file**. Base64 always starts `JVBERi0` = `%PDF-`:

| base64 prefix | decodes to |
|---|---|
| `JVBERi0xLjQK` | `%PDF-1.4\n` |
| `MSAwIG9iajw8L1R5cGUvQ2F0YWxvZy9QYWdlcyAy` | `1 0 obj<</Type/Catalog/Pages 2` |

Decoded head of the captured blob: `'%PDF-1.4\n%âãÏÓ\n1 0 obj<</Type/Catalog/Pa'`

**All biggest strings during upload (unfiltered — note the PDF vs the module noise):**

- `160.0 MB` — [{"version":1,"startData":"1","resultData":"2","executionData":"3","re
- `160.0 MB` — JVBERi0xLjQKJeLjz9MKMSAwIG9iajw8L1R5cGUvQ2F0YWxvZy9QYWdlcyAyIDAgUj4+Cm  ⬅ **the PDF**
- `1.4 MB` — // AUTO-GENERATED — do not edit\n// Pre-compiled ajv validator for the
- `1.0 MB` — /**\n * @license\n * Lodash <https://lodash.com/>\n * Copyright OpenJS
- `0.7 MB` — "use strict";\nvar __defProp = Object.defineProperty;\nvar __getOwnPro
- `0.5 MB` — 'use strict';\n\nObject.defineProperty(exports, '__esModule', { value:

---

## What this means

- With `N8N_DEFAULT_BINARY_DATA_MODE=default`, an uploaded file lives in the heap as a **base64 string** (~1.37× the file size) while the execution is processed and written to Postgres.
- A 120 MB PDF → **160 MB string** on the heap. The heap climbs from ~150 MB toward the 512 MB cap; the single largest object is the file itself.
- Scale the file (or concurrency) up and that string pushes the heap past the cap → **`JavaScript heap out of memory`**. That is the OOM, seen at the object level.

*Reproduce: on http://localhost:8888 set **capture in** = 2 s, click **Capture raw heap**, upload to `http://localhost:8080/webhook/upload`, then tick **PDF upload only**.*
