"""Shared bits for the requests scenarios (violations, ARC, maintenance)."""

import base64

# 1x1 transparent PNG
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def png(tmp_path, name="photo.png") -> str:
    p = tmp_path / name
    p.write_bytes(PNG)
    return str(p)


def pdf(tmp_path, name="plan.pdf") -> str:
    p = tmp_path / name
    p.write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n")
    return str(p)
