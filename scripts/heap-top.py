#!/usr/bin/env python3
"""
Dump the RAW contents of a V8 .heapsnapshot: which object types dominate the
heap (by shallow self-size + count) and the actual biggest STRING values still
resident. This is the "what is literally inside the heap" view, from the CLI —
no Chrome needed.

Usage:
    scripts/heap-top.py heapsnapshots/<file>.heapsnapshot [TOP]

Notes:
  * self_size is SHALLOW size (the object itself, not what it retains). Summing
    it per type is the honest "who is eating the heap" ranking.
  * String node 'name' is the string's own text, so we can print real contents.
"""
import json
import sys

args = [a for a in sys.argv[1:] if a != "--pdf"]
PDF_ONLY = "--pdf" in sys.argv          # show only uploaded-PDF data (base64 "JVBERi…" / "%PDF…")
path = args[0]
TOP = int(args[1]) if len(args) > 1 else 25

with open(path, "rb") as f:
    snap = json.load(f)

meta = snap["snapshot"]["meta"]
node_fields = meta["node_fields"]
node_types = meta["node_types"][0]        # first field is the enum of node types
nodes = snap["nodes"]
strings = snap["strings"]

F = len(node_fields)
i_type = node_fields.index("type")
i_name = node_fields.index("name")
i_self = node_fields.index("self_size")

by_type = {}          # type -> [count, total_self]
by_ctor = {}          # constructor/name -> [count, total_self]  (for objects)
big_strings = []      # (self_size, text)

n = len(nodes)
for off in range(0, n, F):
    t = node_types[nodes[off + i_type]]
    name = strings[nodes[off + i_name]]
    self = nodes[off + i_self]

    d = by_type.setdefault(t, [0, 0]); d[0] += 1; d[1] += self

    if t in ("object", "closure", "array"):
        c = by_ctor.setdefault((t, name), [0, 0]); c[0] += 1; c[1] += self
    if t in ("string", "concatenated string", "sliced string") and self > 4096:
        if not PDF_ONLY or name.startswith("JVBERi") or name.startswith("%PDF") or "application/pdf" in name[:400]:
            big_strings.append((self, name))

total_self = sum(v[1] for v in by_type.values())
mb = lambda b: f"{b/1048576:9.1f} MB"

print(f"\n=== {path}")
print(f"    {n//F:,} nodes · total shallow heap {mb(total_self)}\n")

print("── heap by NODE TYPE (shallow self-size) " + "─" * 24)
print(f"{'type':<22}{'count':>12}{'self-size':>14}   share")
for t, (c, s) in sorted(by_type.items(), key=lambda x: -x[1][1]):
    bar = "█" * int(30 * s / total_self) if total_self else ""
    print(f"{t:<22}{c:>12,}{mb(s):>14}   {bar}")

print("\n── top OBJECT constructors / arrays (shallow) " + "─" * 19)
print(f"{'type:constructor':<40}{'count':>12}{'self-size':>14}")
for (t, name), (c, s) in sorted(by_ctor.items(), key=lambda x: -x[1][1])[:TOP]:
    label = f"{t}:{name or '(anon)'}"
    print(f"{label:<40}{c:>12,}{mb(s):>14}")

print(f"\n── biggest RESIDENT STRINGS (raw contents, top {TOP}) " + "─" * 12)
big_strings.sort(reverse=True)
for s, text in big_strings[:TOP]:
    preview = text.replace("\n", "\\n")
    if len(preview) > 100:
        preview = preview[:100] + f"…(+{len(text)-100} chars)"
    print(f"{mb(s)}  {preview!r}")
if not big_strings:
    print("  (no strings larger than 4 KB)")
print()
