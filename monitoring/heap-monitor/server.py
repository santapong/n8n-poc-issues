#!/usr/bin/env python3
"""
Realtime heap / memory monitor for the n8n lab.

Scrapes each n8n node's Prometheus /metrics (V8 heap, external memory) AND the
real container memory from the Docker Engine API (unix socket). Serves a live
dashboard at :8888 that polls its own same-origin /api/heap endpoint (no CORS).

Stdlib only — no pip install needed.
"""
import json
import os
import re
import socket
import http.client
from urllib.parse import quote
from urllib.request import urlopen
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAIN_METRICS = os.environ.get("MAIN_METRICS", "http://n8n-main:5678/metrics")
WORKER_METRICS = os.environ.get("WORKER_METRICS", "http://n8n-worker:5678/metrics")
DOCKER_SOCK = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")
HEAP_CAP_MB = int(os.environ.get("HEAP_CAP_MB", "512"))
MEM_LIMIT_MB = int(os.environ.get("MEM_LIMIT_MB", "1024"))
PORT = int(os.environ.get("PORT", "8888"))

# metric name -> key we expose
METRICS = {
    "n8n_nodejs_heap_size_used_bytes": "heapUsed",
    "n8n_nodejs_heap_size_total_bytes": "heapTotal",
    "n8n_nodejs_external_memory_bytes": "external",
}


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=3):
        super().__init__("localhost")
        self._unix_path = path
        self._to = timeout

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self._to)
        s.connect(self._unix_path)
        self.sock = s


def docker_get(path):
    conn = UnixHTTPConnection(DOCKER_SOCK)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return json.loads(body)


def container_id_for(service):
    """Resolve a container id by its compose service label."""
    flt = quote(json.dumps({"label": [f"com.docker.compose.service={service}"]}))
    arr = docker_get(f"/v1.43/containers/json?filters={flt}")
    return arr[0]["Id"] if arr else None


def container_mem(service):
    """Real used memory in bytes (docker-CLI style: usage - inactive_file)."""
    try:
        cid = container_id_for(service)
        if not cid:
            return None
        st = docker_get(f"/v1.43/containers/{cid}/stats?stream=false")
        ms = st.get("memory_stats", {})
        usage = ms.get("usage")
        if usage is None:
            return None
        inactive = ms.get("stats", {}).get("inactive_file", 0)
        return max(0, usage - inactive)
    except Exception:
        return None


SPACE_RE = re.compile(r'n8n_nodejs_heap_space_size_used_bytes\{space="([^"]+)"\}\s+([0-9.eE+]+)')


def scrape_metrics(url):
    out = {"heapUsed": None, "heapTotal": None, "external": None, "up": False, "spaces": {}}
    try:
        with urlopen(url, timeout=2) as r:
            text = r.read().decode("utf-8", "replace")
        out["up"] = True
        for line in text.splitlines():
            for name, key in METRICS.items():
                if line.startswith(name + " "):
                    try:
                        out[key] = float(line.split()[1])
                    except (IndexError, ValueError):
                        pass
            m = SPACE_RE.match(line)
            if m:
                try:
                    out["spaces"][m.group(1)] = float(m.group(2))
                except ValueError:
                    pass
    except Exception:
        pass
    return out


import io
import struct
import tarfile

# ── on-demand RAW HEAP capture (heap snapshot → parsed summary) ──────────────
TRIGGER_SH = r'''
pid=$(for p in $(pgrep node); do pp=$(awk '{print $4}' /proc/$p/stat 2>/dev/null); [ "$pp" = "1" ] && echo $p; done | head -1)
[ -z "$pid" ] && { echo "ERR:nopid"; exit 1; }
rm -f /home/node/*.heapsnapshot
kill -USR2 "$pid"
f=""; i=0
while [ $i -lt 60 ]; do
  f=$(ls -1t /home/node/*.heapsnapshot 2>/dev/null | head -1)
  if [ -n "$f" ]; then
    s1=$(wc -c < "$f" 2>/dev/null); sleep 0.6; s2=$(wc -c < "$f" 2>/dev/null)
    [ "$s1" = "$s2" ] && [ "${s1:-0}" -gt 0 ] && break
  else sleep 0.5; fi
  i=$((i+1))
done
echo "PATH:$f"
'''


def docker_exec(cid, cmd):
    """Run a command in a container via the Engine API, return decoded stdout."""
    payload = json.dumps({"AttachStdout": True, "AttachStderr": False,
                          "Tty": False, "Cmd": cmd}).encode()
    conn = UnixHTTPConnection(DOCKER_SOCK, timeout=120)
    conn.request("POST", f"/v1.43/containers/{cid}/exec",
                 body=payload, headers={"Content-Type": "application/json"})
    exec_id = json.loads(conn.getresponse().read())["Id"]
    conn.close()

    conn = UnixHTTPConnection(DOCKER_SOCK, timeout=120)
    body = json.dumps({"Detach": False, "Tty": False}).encode()
    conn.request("POST", f"/v1.43/exec/{exec_id}/start",
                 body=body, headers={"Content-Type": "application/json"})
    raw = conn.getresponse().read()
    conn.close()
    # de-multiplex the docker stream (8-byte header frames)
    out, i = [], 0
    while i + 8 <= len(raw):
        stream, size = raw[i], struct.unpack(">I", raw[i + 4:i + 8])[0]
        chunk = raw[i + 8:i + 8 + size]
        if stream == 1:
            out.append(chunk)
        i += 8 + size
    return b"".join(out).decode("utf-8", "replace")


def docker_fetch_file(cid, path):
    """GET a single file from a container as raw bytes (via the archive tar)."""
    conn = UnixHTTPConnection(DOCKER_SOCK, timeout=120)
    conn.request("GET", f"/v1.43/containers/{cid}/archive?path={quote(path)}")
    resp = conn.getresponse()
    tar_bytes = resp.read()
    conn.close()
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tf:
        member = tf.getmembers()[0]
        return tf.extractfile(member).read()


def parse_snapshot(raw):
    snap = json.loads(raw)
    meta = snap["snapshot"]["meta"]
    nf = meta["node_fields"]
    node_types = meta["node_types"][0]
    nodes, strings = snap["nodes"], snap["strings"]
    F, it, inm, iss = len(nf), nf.index("type"), nf.index("name"), nf.index("self_size")
    by_type, by_ctor, big = {}, {}, []
    for off in range(0, len(nodes), F):
        t = node_types[nodes[off + it]]
        name = strings[nodes[off + inm]]
        self = nodes[off + iss]
        d = by_type.setdefault(t, [0, 0]); d[0] += 1; d[1] += self
        if t in ("object", "closure", "array"):
            c = by_ctor.setdefault(f"{t}:{name or '(anon)'}", [0, 0]); c[0] += 1; c[1] += self
        if t in ("string", "concatenated string", "sliced string") and self > 4096:
            big.append((self, name))
    total = sum(v[1] for v in by_type.values()) or 1
    mb = lambda b: round(b / 1048576, 1)
    big.sort(reverse=True)
    def prev(s):
        s = s.replace("\n", "\\n")
        return s[:120] + (f"…(+{len(s)-120})" if len(s) > 120 else "")
    # A PDF upload sits in the heap as either the decoded bytes ("%PDF…") or,
    # far more commonly, as base64 — and base64 of "%PDF-" always starts "JVBERi".
    def is_pdf(t):
        return t.startswith("JVBERi") or t.startswith("%PDF") or "application/pdf" in t[:400]
    pdf = [(s, t) for s, t in big if is_pdf(t)]
    pdf_bytes = sum(s for s, _ in pdf)
    return {
        "nodes": len(nodes) // F,
        "totalMB": mb(total),
        "byType": [{"type": t, "count": c, "mb": mb(s), "pct": round(100 * s / total)}
                   for t, (c, s) in sorted(by_type.items(), key=lambda x: -x[1][1])],
        "byCtor": [{"label": k, "count": c, "mb": mb(s)}
                   for k, (c, s) in sorted(by_ctor.items(), key=lambda x: -x[1][1])[:15]],
        "bigStrings": [{"mb": mb(s), "text": prev(t)} for s, t in big[:20]],
        "pdfStrings": [{"mb": mb(s), "text": prev(t)} for s, t in pdf[:20]],
        "pdfCount": len(pdf),
        "pdfMB": mb(pdf_bytes),
    }


def capture_raw_heap(service):
    cid = container_id_for(service)
    if not cid:
        return {"error": f"container for '{service}' not found"}
    out = docker_exec(cid, ["sh", "-c", TRIGGER_SH])
    line = [l for l in out.splitlines() if l.startswith("PATH:")]
    if not line or not line[0][5:]:
        return {"error": "snapshot did not appear (is --heapsnapshot-signal=SIGUSR2 set?)", "raw": out[:200]}
    path = line[0][5:]
    raw = docker_fetch_file(cid, path)
    summary = parse_snapshot(raw)
    summary["service"] = service
    summary["snapshotMB"] = round(len(raw) / 1048576, 1)
    try:
        docker_exec(cid, ["sh", "-c", f"rm -f '{path}'"])
    except Exception:
        pass
    return summary


GC_SH = r'''
pid=$(for p in $(pgrep node); do pp=$(awk '{print $4}' /proc/$p/stat 2>/dev/null); [ "$pp" = "1" ] && echo $p; done | head -1)
[ -z "$pid" ] && { echo "ERR:nopid"; exit 1; }
kill -USR2 "$pid"
f=""; i=0
while [ $i -lt 40 ]; do
  f=$(ls -1t /home/node/*.heapsnapshot 2>/dev/null | head -1)
  if [ -n "$f" ]; then s1=$(wc -c < "$f" 2>/dev/null); sleep 0.6; s2=$(wc -c < "$f" 2>/dev/null)
    [ "$s1" = "$s2" ] && [ "${s1:-0}" -gt 0 ] && break; else sleep 0.4; fi
  i=$((i+1))
done
rm -f /home/node/*.heapsnapshot
echo OK
'''


def force_gc(service):
    """Force a full V8 GC: a heap snapshot does a mark-compact as a side effect.
    We trigger it, then delete the snapshot file. Returns heap before/after."""
    url = WORKER_METRICS if service == "n8n-worker" else MAIN_METRICS
    before = scrape_metrics(url).get("heapUsed")
    cid = container_id_for(service)
    if not cid:
        return {"error": f"container for '{service}' not found"}
    out = docker_exec(cid, ["sh", "-c", GC_SH])
    if "OK" not in out:
        return {"error": "gc trigger failed", "raw": out[:200]}
    after = scrape_metrics(url).get("heapUsed")
    mb = lambda b: round(b / 1048576) if b else None
    return {"service": service, "beforeMB": mb(before), "afterMB": mb(after),
            "freedMB": (mb(before) - mb(after)) if (before and after) else None}


def snapshot():
    main = scrape_metrics(MAIN_METRICS)
    worker = scrape_metrics(WORKER_METRICS)
    main["rss"] = container_mem("n8n-main")
    worker["rss"] = container_mem("n8n-worker")
    return {
        "heapCapBytes": HEAP_CAP_MB * 1024 * 1024,
        "memLimitBytes": MEM_LIMIT_MB * 1024 * 1024,
        "main": main,
        "worker": worker,
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/gc"):
            service = "n8n-worker" if "worker" in self.path else "n8n-main"
            try:
                body = json.dumps(force_gc(service)).encode()
            except Exception as e:
                body = json.dumps({"error": str(e)}).encode()
            self._send(200, body, "application/json")
            return
        if self.path.startswith("/api/heapraw"):
            node = "worker" if "worker" in self.path else "main"
            service = "n8n-worker" if node == "worker" else "n8n-main"
            try:
                body = json.dumps(capture_raw_heap(service)).encode()
            except Exception as e:
                body = json.dumps({"error": str(e)}).encode()
            self._send(200, body, "application/json")
            return
        if self.path.startswith("/api/heap"):
            body = json.dumps(snapshot()).encode()
            self._send(200, body, "application/json")
            return
        # serve dashboard
        try:
            with open(os.path.join(os.path.dirname(__file__), "index.html"), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        except FileNotFoundError:
            self._send(404, b"index.html missing", "text/plain")


if __name__ == "__main__":
    print(f"heap-monitor listening on :{PORT}  (main={MAIN_METRICS} worker={WORKER_METRICS})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
