"""QR rendering for ``tg://login`` tokens (QR login, no SMS and no code).

``segno`` is a dependency-free QR encoder, so this stays a pure-Python affair.
If it is missing the caller still gets the raw ``tg://`` URL to click.
"""
from __future__ import annotations

import base64
import io
import logging
from typing import Optional

log = logging.getLogger("tegrab")

try:  # pragma: no cover - the import is the only failure mode
    import segno
except Exception:  # pragma: no cover
    segno = None


def data_uri(text: str, *, scale: int = 6, border: int = 2) -> Optional[str]:
    """PNG ``data:`` URI for *text*, or ``None`` when no encoder is available."""
    if segno is None:
        return None
    try:
        buf = io.BytesIO()
        segno.make(text, error="m").save(buf, kind="png", scale=scale, border=border)
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as exc:  # pragma: no cover
        log.warning("qr render failed: %r", exc)
        return None
