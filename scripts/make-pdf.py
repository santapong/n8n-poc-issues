#!/usr/bin/env python3
"""
Generate a REAL, valid, renderable one-page PDF with correct xref offsets.
Optionally pad it to an approximate target size (for large-upload tests) by
appending a binary stream object — the file stays a valid, openable PDF.

Usage:
    scripts/make-pdf.py out.pdf                # small valid 1-page PDF
    scripts/make-pdf.py big.pdf 80             # ~80 MB valid 1-page PDF
"""
import os
import sys

out = sys.argv[1]
target_mb = float(sys.argv[2]) if len(sys.argv) > 2 else 0

text = b"HELLO-N8N-PDF-MARKER-12345  --  this is a real one-page PDF"
content = b"BT /F1 20 Tf 60 720 Td (" + text + b") Tj ET"

objs = [
    b"<</Type/Catalog/Pages 2 0 R>>",
    b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
    b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
    b"/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>",
    b"<</Length %d>>stream\n%s\nendstream" % (len(content), content),
    b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
]

# Optional big padding stream object (object 6) to reach the target size.
# It is a valid, self-describing stream; viewers simply don't reference it.
pad_len = 0
if target_mb:
    # rough base size without padding; then size the pad to hit the target
    base = 400 + sum(len(o) + 20 for o in objs) + 200
    pad_len = max(0, int(target_mb * 1024 * 1024) - base)
    objs.append(b"<</Length %d>>stream\n" % pad_len + os.urandom(pad_len) + b"\nendstream")

buf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")   # binary comment => treated as binary
offsets = []
for i, body in enumerate(objs, start=1):
    offsets.append(len(buf))
    buf += b"%d 0 obj" % i + body + b"\nendobj\n"

xref_pos = len(buf)
n = len(objs) + 1
buf += b"xref\n0 %d\n" % n
buf += b"0000000000 65535 f \n"
for off in offsets:
    buf += b"%010d 00000 n \n" % off
buf += b"trailer\n<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF\n" % (n, xref_pos)

with open(out, "wb") as f:
    f.write(buf)
print(f"wrote {out}  {len(buf)/1048576:.1f} MB  ({len(objs)} objects, pad={pad_len} bytes)")
