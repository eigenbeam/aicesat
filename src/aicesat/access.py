"""Tier-1 read primitives (spec §6.2–6.3): presigned-URL resolution, concurrent HTTPS byte-range GETs, chunk decode.

Nothing here opens or parses an HDF5 file. Byte ranges and filter pipelines come from the index (index.py),
decode is zlib (stdlib) + the HDF5 byte-shuffle transpose in numpy, validated byte-identical against h5py
(scripts/probe_range_get.py). Every request and byte is counted for the access scoreboard.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import numpy as np
import requests

from . import auth

log = logging.getLogger(__name__)
PRESIGN_TTL_S = 45 * 60


@dataclass
class AccessStats:
    requests: int = 0
    bytes: int = 0
    seconds: float = 0.0
    chunks: int = 0
    presigns: int = 0
    granules_touched: set = field(default_factory=set)
    hdf5_opens: int = 0          # always 0 on this path; the number the comparison is about
    structure_parses: int = 0    # idem

    def as_dict(self) -> dict:
        return {"requests": self.requests, "bytes": self.bytes, "seconds": round(self.seconds, 2), "chunks": self.chunks,
                "presigns": self.presigns, "granules_touched": len(self.granules_touched),
                "hdf5_opens_at_query_time": self.hdf5_opens, "structure_parses_at_query_time": self.structure_parses}


class RangeReader:
    """Concurrent byte-range reader over Earthdata Cloud HTTPS URLs (bearer token -> 303 -> presigned CloudFront)."""

    def __init__(self, threads: int = 8):
        auth.login()
        self.token = os.environ["EARTHDATA_TOKEN"]
        self.session = requests.Session()
        self.threads = threads
        self.stats = AccessStats()
        self._presigned: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def presigned(self, url: str, refresh: bool = False) -> str:
        with self._lock:
            hit = self._presigned.get(url)
            if hit and not refresh and time.time() - hit[1] < PRESIGN_TTL_S:
                return hit[0]
        r = self.session.get(url, headers={"Authorization": f"Bearer {self.token}", "Range": "bytes=0-0"}, allow_redirects=True, timeout=60)
        r.raise_for_status()
        with self._lock:
            self._presigned[url] = (r.url, time.time())
            self.stats.presigns += 1
            self.stats.requests += 1
        return r.url

    def _get(self, purl: str, offset: int, size: int) -> bytes:
        r = self.session.get(purl, headers={"Range": f"bytes={offset}-{offset + size - 1}"}, timeout=120)
        if r.status_code != 206 or len(r.content) != size:
            raise IOError(f"range GET failed: status {r.status_code}, got {len(r.content)} of {size} bytes")
        return r.content

    def fetch(self, url: str, ranges: list[tuple[int, int]]) -> list[bytes]:
        """Fetch [(offset, size), ...] from one granule concurrently; returns raw (still compressed) chunk bytes in order."""
        t0 = time.time()
        purl = self.presigned(url)
        try:
            with ThreadPoolExecutor(self.threads) as ex:
                out = list(ex.map(lambda r: self._get(purl, *r), ranges))
        except IOError:
            purl = self.presigned(url, refresh=True)  # presigned URL may have expired: refresh once and retry
            with ThreadPoolExecutor(self.threads) as ex:
                out = list(ex.map(lambda r: self._get(purl, *r), ranges))
        with self._lock:
            self.stats.requests += len(ranges)
            self.stats.bytes += sum(len(b) for b in out)
            self.stats.chunks += len(ranges)
            self.stats.seconds += time.time() - t0
            self.stats.granules_touched.add(url)
        return out


def unshuffle(buf: bytes, itemsize: int) -> bytes:
    """Undo the HDF5 byte-shuffle filter (id 2): the stored layout is all first bytes, then all second bytes, ..."""
    a = np.frombuffer(buf, dtype="u1")
    n = a.size // itemsize
    return a[: n * itemsize].reshape(itemsize, n).T.copy().tobytes()


SUPPORTED_FILTERS = {"gzip", "shuffle"}


def decode_chunk(raw: bytes, dtype: str, filters: str, ncols: int = 1, filter_mask: int = 0) -> np.ndarray:
    """raw chunk bytes -> array. filters is the index's pipeline string in WRITE order, e.g. 'gzip' or 'shuffle,gzip'.
    filter_mask is HDF5's per-chunk mask: bit i set => filter i of the pipeline was skipped for this chunk (e.g. a
    chunk that did not shrink is stored without deflate)."""
    steps = [s for s in filters.split(",") if s]
    bad = [s for s in steps if s not in SUPPORTED_FILTERS]
    if bad:
        raise ValueError(f"unsupported HDF5 filter(s) {bad}; refuse to guess (spec §6.3)")
    buf = raw
    for i in reversed(range(len(steps))):  # reads undo the pipeline in reverse write order
        if filter_mask & (1 << i):
            continue
        if steps[i] == "gzip":
            buf = zlib.decompress(buf)
        elif steps[i] == "shuffle":
            buf = unshuffle(buf, np.dtype(dtype).itemsize)
    arr = np.frombuffer(buf, dtype=np.dtype(dtype))
    return arr.reshape(-1, ncols) if ncols > 1 else arr
