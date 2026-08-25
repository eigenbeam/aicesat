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
    gap_bytes: int = 0           # bytes fetched only because they sat between two wanted ranges (coalescing cost)
    spans: int = 0               # coalesced spans actually requested
    granules_touched: set = field(default_factory=set)
    hdf5_opens: int = 0          # always 0 on this path; the number the comparison is about
    structure_parses: int = 0    # idem

    def as_dict(self) -> dict:
        return {"requests": self.requests, "bytes": self.bytes, "seconds": round(self.seconds, 2), "chunks": self.chunks,
                "spans": self.spans, "gap_bytes": self.gap_bytes,
                "presigns": self.presigns, "granules_touched": len(self.granules_touched),
                "hdf5_opens_at_query_time": self.hdf5_opens, "structure_parses_at_query_time": self.structure_parses}


MAX_GAP_BYTES = 256 * 1024   # merge two wanted ranges into one GET if the unwanted gap between them is smaller than this
MAX_SPAN_BYTES = 64 << 20    # keep individual GETs bounded so threads still overlap


def coalesce(ranges: list[tuple[int, int]], max_gap: int = MAX_GAP_BYTES, max_span: int = MAX_SPAN_BYTES) -> list[tuple[int, int]]:
    """Merge (offset, size) ranges that are adjacent or separated by less than max_gap into spans (offset, size).
    Consecutive HDF5 chunks of one dataset are byte-contiguous in ATL03 (verified), so a run of wanted chunks becomes
    ONE request; S3/CloudFront do not support multi-range requests, so this is the only way to cut round-trips."""
    if not ranges:
        return []
    out = []
    for off, size in sorted(ranges):
        if out:
            so, ss = out[-1]
            gap = off - (so + ss)
            if gap <= max_gap and (max(so + ss, off + size) - so) <= max_span:
                out[-1] = (so, max(so + ss, off + size) - so)
                continue
        out.append((off, size))
    return out


class RangeReader:
    """Concurrent byte-range reader over Earthdata Cloud HTTPS URLs (bearer token -> 303 -> presigned CloudFront).
    Wanted ranges are coalesced into spans before fetching; callers still get exactly the bytes they asked for."""

    def __init__(self, threads: int = 8, max_gap: int = MAX_GAP_BYTES):
        self.max_gap = max_gap
        auth.login()
        self.token = os.environ["EARTHDATA_TOKEN"]
        self.session = requests.Session()
        # NSIDC/CloudFront return transient 5xx on a small fraction of range GETs; requests' default retries none of them.
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        retry = Retry(total=5, backoff_factor=0.6, status_forcelist=(429, 500, 502, 503, 504),
                      allowed_methods=frozenset({"GET"}), respect_retry_after_header=True)
        self.session.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=32, pool_maxsize=32))
        self.threads = threads
        self.stats = AccessStats()
        self._presigned: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def presigned(self, url: str, refresh: bool = False) -> str:
        with self._lock:  # held across the resolve so concurrent callers never presign the same granule twice
            hit = self._presigned.get(url)
            if hit and not refresh and time.time() - hit[1] < PRESIGN_TTL_S:
                return hit[0]
            r = self.session.get(url, headers={"Authorization": f"Bearer {self.token}", "Range": "bytes=0-0"}, allow_redirects=True, timeout=60)
            r.raise_for_status()
            self._presigned[url] = (r.url, time.time())
            self.stats.presigns += 1
            self.stats.requests += 1
            return r.url

    def _get(self, purl: str, offset: int, size: int) -> bytes:
        r = self.session.get(purl, headers={"Range": f"bytes={offset}-{offset + size - 1}"}, timeout=120)
        if r.status_code == 200:  # server ignored Range and sent the whole object: slice, but account for the full cost
            with self._lock:
                self.stats.gap_bytes += len(r.content) - size
            return r.content[offset: offset + size]
        if r.status_code == 403:
            raise IOError("403: presigned URL expired")
        if r.status_code != 206 or len(r.content) != size:
            raise IOError(f"range GET failed: status {r.status_code}, got {len(r.content)} of {size} bytes")
        return r.content

    def fetch(self, url: str, ranges: list[tuple[int, int]]) -> list[bytes]:
        """Fetch [(offset, size), ...] from one granule; returns the raw (still compressed) bytes of each range, in order.
        Adjacent/near ranges are fetched as one span and sliced apart."""
        t0 = time.time()
        spans = coalesce(ranges, self.max_gap)
        purl = self.presigned(url)
        try:
            with ThreadPoolExecutor(self.threads) as ex:
                blobs = list(ex.map(lambda r: self._get(purl, *r), spans))
        except IOError:
            purl = self.presigned(url, refresh=True)  # presigned URL may have expired: refresh once and retry
            with ThreadPoolExecutor(self.threads) as ex:
                blobs = list(ex.map(lambda r: self._get(purl, *r), spans))
        out = []
        for off, size in ranges:
            for (so, ss), blob in zip(spans, blobs):
                if so <= off and off + size <= so + ss:
                    out.append(blob[off - so: off - so + size])
                    break
            else:
                raise RuntimeError(f"range ({off}, {size}) not covered by any span")  # cannot happen if coalesce is right
        wanted = sum(s for _, s in ranges)
        got = sum(len(b) for b in blobs)
        with self._lock:
            self.stats.requests += len(spans)
            self.stats.spans += len(spans)
            self.stats.bytes += got
            self.stats.gap_bytes += got - wanted
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
