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


# Coalescing gap: merge two wanted ranges into one GET if the unwanted gap between them is smaller than this.
# The optimum depends on the bandwidth-delay product of the link: NSIDC measured 256 KB in-region (10-30 ms TTFB);
# from a remote laptop (100-160 ms TTFB, ~40 MB/s) requests under ~6 MB are latency-bound, so a larger gap that builds
# multi-MB spans wins. Override with AICESAT_COALESCE_GAP (bytes).
MAX_GAP_BYTES = int(os.environ.get("AICESAT_COALESCE_GAP", 2 << 20))
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


def in_region() -> bool:
    """True where NSIDC S3 direct access works (us-west-2): reads go straight to S3 with STS creds — no presign, no
    CloudFront hop, no egress charge. AICESAT_S3_DIRECT=1/0 forces it on/off (tests, or a non-standard region var)."""
    o = os.environ.get("AICESAT_S3_DIRECT")
    if o is not None:
        return o == "1"
    return "us-west-2" in os.environ.get("AWS_REGION", "") or "us-west-2" in os.environ.get("AWS_DEFAULT_REGION", "")


_S3_CREDS: dict = {"v": None, "exp": 0.0}
_S3_CRED_LOCK = threading.Lock()


def s3_credentials(refresh: bool = False) -> dict:
    """NSIDC temporary S3 credentials (accessKeyId/secretAccessKey/sessionToken), valid ~1 h, cached and refreshed.
    Only meaningful in-region; the STS-scoped creds are rejected out-of-region."""
    with _S3_CRED_LOCK:
        if not refresh and _S3_CREDS["v"] and time.time() < _S3_CREDS["exp"]:
            return _S3_CREDS["v"]
        import earthaccess
        auth.login()
        _S3_CREDS["v"] = earthaccess.get_s3_credentials(daac="NSIDC")
        _S3_CREDS["exp"] = time.time() + 3000   # refresh comfortably before the ~1 h expiry
        return _S3_CREDS["v"]


def access_url(https_url: str, s3_url: str | None) -> str:
    """The URL to byte-range at run time: S3-direct in-region (no presign, no egress), else HTTPS/CloudFront.
    The index stores both, so a query picks the fast path for wherever it runs."""
    return s3_url if (in_region() and s3_url) else https_url


class RangeReader:
    """Concurrent byte-range reader over Earthdata Cloud granules. Out-of-region: bearer -> 303 -> presigned CloudFront
    HTTPS GETs. In-region (s3:// URLs): boto3 range GETs against nsidc-cumulus S3 with STS creds — no presign, no
    egress. Wanted ranges are coalesced into spans before fetching; callers still get exactly the bytes they asked for."""

    def __init__(self, threads: int | None = None, max_gap: int | None = None):
        reg = in_region()
        # In-region the round trip is ~10-30 ms and egress is free, so a SMALL coalescing gap wins (less over-fetch);
        # from a remote laptop the ~150 ms TTFB makes a large gap (fewer round trips) win. Override: AICESAT_COALESCE_GAP.
        self.max_gap = (max_gap if max_gap is not None else
                        int(os.environ["AICESAT_COALESCE_GAP"]) if os.environ.get("AICESAT_COALESCE_GAP")
                        else (256 << 10 if reg else 2 << 20))
        auth.login()
        self.token = os.environ["EARTHDATA_TOKEN"]
        self.session = requests.Session()
        # NSIDC/CloudFront return transient 5xx on a small fraction of range GETs; requests' default retries none of them.
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        retry = Retry(total=5, backoff_factor=0.6, status_forcelist=(429, 500, 502, 503, 504),
                      allowed_methods=frozenset({"GET"}), respect_retry_after_header=True)
        self.session.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=32, pool_maxsize=32))
        self.threads = threads if threads is not None else (16 if reg else 8)   # S3 scales; in-region go wider
        self.stats = AccessStats()
        self._presigned: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()
        self._url_locks: dict[str, threading.Lock] = {}  # one lock per granule URL: presigns for different granules overlap
        self._s3 = None                                  # lazily-built boto3 client (in-region S3-direct)

    def _s3fs(self):
        if self._s3 is None:
            import s3fs
            c = s3_credentials()
            self._s3 = s3fs.S3FileSystem(key=c["accessKeyId"], secret=c["secretAccessKey"], token=c["sessionToken"])
        return self._s3

    def _get_s3(self, s3url: str, offset: int, size: int) -> bytes:
        try:
            b = self._s3fs().cat_file(s3url, start=offset, end=offset + size)   # end is exclusive (fsspec)
        except Exception as e:                           # STS creds expire ~hourly: refresh once and retry
            if "ExpiredToken" in str(e) or "InvalidAccessKeyId" in str(e):
                self._s3 = None
                s3_credentials(refresh=True)
                b = self._s3fs().cat_file(s3url, start=offset, end=offset + size)
            else:
                raise
        if len(b) != size:
            raise IOError(f"s3 range GET short: got {len(b)} of {size} bytes")
        return b

    def _getter(self, url: str, refresh: bool = False):
        """A callable (offset, size) -> bytes for one granule: S3-direct for s3:// URLs, presigned HTTPS otherwise."""
        if url.startswith("s3://"):
            return lambda off, size: self._get_s3(url, off, size)
        purl = self.presigned(url, refresh=refresh)
        return lambda off, size: self._get(purl, off, size)

    def presigned(self, url: str, refresh: bool = False) -> str:
        with self._lock:
            ulock = self._url_locks.setdefault(url, threading.Lock())
        with ulock:  # per-URL: the same granule is never presigned twice concurrently, different granules proceed in parallel
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

    def presign_all(self, urls) -> dict[str, str]:
        """Resolve many granule URLs concurrently (each is an EDL round trip of ~1-2 s from outside the region)."""
        with ThreadPoolExecutor(min(8, max(1, len(urls)))) as ex:
            return dict(zip(urls, ex.map(self.presigned, urls)))

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
        get = self._getter(url)
        try:
            with ThreadPoolExecutor(self.threads) as ex:
                blobs = list(ex.map(lambda r: get(*r), spans))
        except IOError:
            get = self._getter(url, refresh=True)  # HTTPS presigned URL may have expired: refresh + retry (S3 self-heals in _get_s3)
            with ThreadPoolExecutor(self.threads) as ex:
                blobs = list(ex.map(lambda r: get(*r), spans))
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

    def read_all(self, url: str) -> bytes:
        """GET an entire Earthdata Cloud object in one request. In-region s3:// -> boto3 get_object; else bearer ->
        presigned CloudFront -> whole-object GET. For small non-chunked files (e.g. ILATM2 CSV) scanned whole at
        index-build time. Never the retired on-prem hosts earthaccess.open can fall back to (n5eil01u)."""
        if url.startswith("s3://"):
            try:
                data = self._s3fs().cat_file(url)
            except Exception as e:
                if "ExpiredToken" in str(e) or "InvalidAccessKeyId" in str(e):
                    self._s3 = None
                    s3_credentials(refresh=True)
                    data = self._s3fs().cat_file(url)
                else:
                    raise
        else:
            resp = self.session.get(self.presigned(url), timeout=120)
            if resp.status_code == 403:
                resp = self.session.get(self.presigned(url, refresh=True), timeout=120)
            resp.raise_for_status()
            data = resp.content
        with self._lock:
            self.stats.requests += 1
            self.stats.bytes += len(data)
        return data


def cloud_hdf5_file(url: str, s3url: str | None = None, block_size: int = 1 << 20, reader: "RangeReader | None" = None):
    """Open an Earthdata Cloud granule as a block-cached fsspec file for h5py. In-region (s3:// available) -> s3fs with
    STS creds, no presign; out-of-region -> a single presign to CloudFront, then range GETs over the presigned URL.
    Use INSTEAD of earthaccess.open at index-build time: its link picker can fall back to retired on-prem hosts (the
    decommissioned n5eil01u). Pass `reader` to share one presign/session with the caller's own byte-range reads."""
    if in_region() and s3url:
        import s3fs
        c = s3_credentials()
        fs = s3fs.S3FileSystem(key=c["accessKeyId"], secret=c["secretAccessKey"], token=c["sessionToken"],
                               default_block_size=block_size)
        return fs.open(s3url)
    import fsspec
    purl = (reader or RangeReader()).presigned(url)
    fs = fsspec.filesystem("https", block_size=block_size)
    return fs.open(purl)


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
