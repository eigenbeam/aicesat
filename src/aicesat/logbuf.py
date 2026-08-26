"""In-memory ring buffer of recent aicesat log records, so the Lake page can show a running activity log of what
the pipeline is doing (CMR search, chunk fetch/decode, materialize, evict, query). Main-process logs only —
ProcessPool workers log to their own processes and don't appear here; the orchestration-level messages do.
"""
from __future__ import annotations

import logging
import threading
from collections import deque

_LOCK = threading.Lock()
_BUF: deque = deque(maxlen=500)
_SEQ = 0
_LOGGERS = ("aicesat.lake", "aicesat.planner", "aicesat.access", "aicesat.index", "aicesat.coverage",
            "aicesat.atl03", "aicesat.glas", "aicesat.atl06", "aicesat.icessn", "aicesat.dem", "aicesat.api")
_installed = False


class _RingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        global _SEQ
        try:
            msg = record.getMessage()
        except Exception:
            return
        with _LOCK:
            _SEQ += 1
            _BUF.append({"seq": _SEQ, "t": record.created, "level": record.levelname,
                         "name": record.name.replace("aicesat.", ""), "msg": msg[:400]})


def install() -> None:
    """Attach the ring handler to the pipeline loggers (idempotent)."""
    global _installed
    if _installed:
        return
    h = _RingHandler()
    h.setLevel(logging.INFO)
    for name in _LOGGERS:
        lg = logging.getLogger(name)
        lg.addHandler(h)
        if lg.level == logging.NOTSET or lg.level > logging.INFO:
            lg.setLevel(logging.INFO)
    _installed = True


def entries(after: int = 0) -> dict:
    """Log entries with seq > after, plus the current max seq (for the next poll)."""
    with _LOCK:
        return {"seq": _SEQ, "entries": [e for e in _BUF if e["seq"] > after]}
