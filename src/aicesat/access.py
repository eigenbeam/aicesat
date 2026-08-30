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


def _env_int(name: str) -> int | None:
    v = os.environ.get(name)
    if not v:
        return None
    try:
        return max(1, int(v))
    except ValueError:
        return None


def pool_size(n_items: int, *, cap: int, min_items: int, env: str, cpu_bound: bool = True) -> int:
    """Number of workers for an embarrassingly-parallel loop over `n_items` independent units.

    Falls back to serial (returns 1) below `min_items` so a tiny box never pays pool overhead. Otherwise
    min(cap, n_items), and min(cap, cpu_count) as well when `cpu_bound`; `env` (an int) overrides the worker count
    (set it to 1 to force serial).

    `cpu_bound=False` skips the cpu_count clamp for pools that spend their time WAITING, not computing. Byte-range
    GETs are the case that matters: a 246 KB S3 read is ~25 ms of mostly time-to-first-byte, so useful concurrency is
    set by latency, not cores. Measured on an 8-vCPU box, the same 1,415 MB leg took 34.8 s of fetch wall at 4
    workers and 18.0 s at 16 — while the clamp would have silently capped it at 8 no matter how high `cap` was set.
    """
    if n_items < max(2, min_items):
        return 1
    override = _env_int(env)
    if override is not None:
        base = override
    else:
        base = min(cap, os.cpu_count() or 1) if cpu_bound else cap
    return max(1, min(base, n_items))


def chunk_bounds(n: int, k: int) -> list[tuple[int, int]]:
    """Split range(n) into at most `k` contiguous [start, end) slices, as even as possible (order preserved).
    Used to batch many tiny independent units into a few worker-sized jobs so per-task dispatch cost stays small."""
    k = max(1, min(k, n))
    base, extra = divmod(n, k)
    out, s = [], 0
    for i in range(k):
        e = s + base + (1 if i < extra else 0)
        if e > s:
            out.append((s, e))
        s = e
    return out


# Per-granule fetch fan-out (fetch_bbox across granules, in index_{atl06,glas,icessn}). Each granule's own fetch
# already uses RangeReader's internal thread pool, so the OUTER pool is kept small: outer x inner concurrency then
# stays within the HTTPS connection pool (pool_maxsize=32). Env AICESAT_FETCH_WORKERS overrides the outer width.
FETCH_WORKER_CAP = 16       # network-latency-bound, not CPU-bound — see pool_size(cpu_bound=False)
FETCH_MIN_GRANULES = 3          # 1-2 granules: fetch serially (a pool would only add overhead)
FETCH_WORKER_ENV = "AICESAT_FETCH_WORKERS"


def in_region() -> bool:
    """True where NSIDC S3 direct access works (us-west-2): reads go straight to S3 with STS creds — no presign, no
    CloudFront hop, no egress charge. AICESAT_S3_DIRECT=1/0 forces it on/off (tests, or a non-standard region var)."""
    o = os.environ.get("AICESAT_S3_DIRECT")
    if o is not None:
        return o == "1"
    return "us-west-2" in os.environ.get("AWS_REGION", "") or "us-west-2" in os.environ.get("AWS_DEFAULT_REGION", "")


def default_coalesce_gap(reg: bool | None = None) -> int:
    """The coalescing gap a RangeReader would use here. Split out of __init__ so a benchmark can report the
    configuration it ran under without building a reader (which would trigger a login)."""
    env = os.environ.get("AICESAT_COALESCE_GAP")
    if env:
        return int(env)
    return (1 << 20) if (in_region() if reg is None else reg) else (2 << 20)


S3_REGION = "us-west-2"          # NSIDC Cumulus S3 + the STS creds are us-west-2 only (in-region == this region)
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
    HTTPS GETs. In-region (s3:// URLs): direct range GETs against nsidc-cumulus S3 with STS creds — no presign, no
    egress — via a selectable mechanism (env AICESAT_S3_FETCH; default "s3fs", see S3_FETCH_MECHANISMS). Wanted ranges
    are coalesced into spans before fetching; callers still get exactly the bytes they asked for."""

    def __init__(self, threads: int | None = None, max_gap: int | None = None):
        reg = in_region()
        # In-region the cost curve has TWO regimes and the old 256 KB default sat on the wrong side of the knee.
        # Measured (scripts/bench_coalesce.py, 921 granules / 22,830 ranges / 591 MB wanted / 16 workers, best of 3):
        #
        #     gap    spans   best s   ms/GET        gap   spans   best s   ms/GET
        #    0.25    5,741     13.6     38.0       1.00   3,969      9.9     39.8
        #    0.50    4,337     10.0     36.8       1.50   3,562      9.9     44.3
        #    0.75    4,126      9.6     37.2       2.00   2,812     11.2     63.5
        #                                          3.00   2,167     12.6     93.0
        #
        # A GET costs a flat ~37 ms up to ~1.5 MB (latency-bound: removing a round trip is nearly free, and in-region
        # egress is free so the extra bytes are only transfer time), then scales with size. 0.5-1.5 MB is one flat
        # plateau — the 4% spread across it is run-to-run noise — so take the middle with margin to both edges rather
        # than the nominal minimum. Override: AICESAT_COALESCE_GAP (bytes).
        self.max_gap = max_gap if max_gap is not None else default_coalesce_gap(reg)
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
        self._s3 = None                                  # lazily-built s3fs filesystem (in-region S3-direct)
        # Pluggable in-region S3 range-GET mechanism (see S3_FETCH_MECHANISMS). Default "s3fs" == the historical path;
        # unset env => unchanged behaviour. Flip the winner in one place: export AICESAT_S3_FETCH=<name>.
        # aiobotocore won the in-region benchmark on both shapes (~26% faster on many-small-GETs, tighter p95 than
        # the s3fs cat_file thread pool), byte-identical. Override with AICESAT_S3_FETCH (s3fs|s3fs_ranges|boto3|crt).
        self.s3_fetch = os.environ.get("AICESAT_S3_FETCH", "aiobotocore")
        self._aio = None                                 # lazily-built aiobotocore loop+client (mechanism "aiobotocore")
        self._boto3 = None                               # lazily-built boto3 client (mechanism "boto3")
        self._crt = None                                 # lazily-built awscrt S3 client (mechanism "crt")

    def _s3fs(self):
        if self._s3 is None:
            # Double-checked lock: under concurrent per-granule fetches the lazy build must happen once (and one
            # thread must not observe a half-built client). Reuses self._lock; the built client is thread-safe to share.
            with self._lock:
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

    # ---- pluggable in-region S3 range-GET dispatch ------------------------------------------------------------------
    def _s3_fetch_spans(self, url: str, spans: list[tuple[int, int]], timings: list | None = None) -> list[bytes]:
        """Fetch already-coalesced (offset, size) spans from one s3:// object via the selected mechanism.
        Returns bytes in span order, identical across mechanisms. `timings` (if a list) collects per-GET seconds
        where the mechanism can measure them (thread-pool ones can; the batched cat_ranges cannot)."""
        name = getattr(self, "s3_fetch", "s3fs")
        mech = S3_FETCH_MECHANISMS.get(name)
        if mech is None:
            raise ValueError(f"unknown AICESAT_S3_FETCH={name!r}; choose from {sorted(S3_FETCH_MECHANISMS)}")
        return mech(self, url, spans, timings)

    def _s3_reset(self):
        """Drop every cached S3 client so the next call rebuilds with fresh STS creds."""
        self._s3 = None
        for attr in ("_aio", "_boto3", "_crt"):
            obj = getattr(self, attr, None)
            if obj is not None and hasattr(obj, "close"):
                try:
                    obj.close()
                except Exception:
                    pass
            setattr(self, attr, None)

    def _s3_with_refresh(self, fn):
        """Run fn(); on an expired-STS error, reset clients + refresh creds and retry once. Used by the non-baseline
        mechanisms (the baseline's _get_s3 already self-heals per GET, so its default path is left untouched)."""
        try:
            return fn()
        except Exception as e:
            if "ExpiredToken" in str(e) or "InvalidAccessKeyId" in str(e):
                self._s3_reset()
                s3_credentials(refresh=True)
                return fn()
            raise

    def _aio_client(self):
        if self._aio is None:
            self._aio = _AioS3(s3_credentials(), max_pool=max(self.threads, 32))
        return self._aio

    def _boto3_client(self):
        if self._boto3 is None:
            import boto3
            from botocore.config import Config
            c = s3_credentials()
            self._boto3 = boto3.client("s3", region_name=S3_REGION,
                                       aws_access_key_id=c["accessKeyId"], aws_secret_access_key=c["secretAccessKey"],
                                       aws_session_token=c["sessionToken"],
                                       config=Config(max_pool_connections=max(self.threads, 32), retries={"max_attempts": 3}))
        return self._boto3

    def _crt_client(self):
        if self._crt is None:
            self._crt = _CrtS3(s3_credentials())
        return self._crt

    def close(self):
        """Release async/CRT resources (background event loop, CRT client). Safe to call more than once. The HTTPS
        session and s3fs filesystem are GC'd normally; this only matters for mechanisms that spin their own loop."""
        for attr in ("_aio", "_crt"):
            obj = getattr(self, attr, None)
            if obj is not None and hasattr(obj, "close"):
                try:
                    obj.close()
                except Exception:
                    pass
            setattr(self, attr, None)

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
        if url.startswith("s3://"):
            # In-region S3-direct: the mechanism (AICESAT_S3_FETCH) owns its own concurrency and cred self-heal.
            # Default "s3fs" reproduces the historical path (cat_file per span in a thread pool) byte-for-byte.
            try:
                blobs = self._s3_fetch_spans(url, spans)
            except IOError:
                blobs = self._s3_fetch_spans(url, spans)   # mirror the single retry the HTTPS path does (rare short read)
        else:
            get = self._getter(url)
            try:
                with ThreadPoolExecutor(self.threads) as ex:
                    blobs = list(ex.map(lambda r: get(*r), spans))
            except IOError:
                get = self._getter(url, refresh=True)  # HTTPS presigned URL may have expired: refresh + retry
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


# =====================================================================================================================
# Pluggable in-region S3 range-GET mechanisms.
#
# Each is an interchangeable way to fetch a set of already-coalesced (offset, size) spans from ONE s3:// object and
# return their bytes in order, byte-identical to every other mechanism. The reader selects one by name via the env var
# AICESAT_S3_FETCH (default "s3fs" == the historical path, so unset changes nothing). Adding/removing a mechanism is a
# one-line edit of S3_FETCH_MECHANISMS below; flipping the production default is one env var (or one line in __init__).
#
#   name          concurrency model                              deps (beyond the always-present s3fs)
#   ------------  ---------------------------------------------  -------------------------------------
#   s3fs          cat_file per span in a ThreadPoolExecutor       (baseline; the current behaviour)
#   s3fs_ranges   fsspec cat_ranges (batched, async internally)   —
#   aiobotocore   async get_object gathered on one event loop     aiobotocore + botocore (installed)
#   boto3         sync get_object in a ThreadPoolExecutor          boto3 (optional; may be absent)
#   crt           awscrt S3 client, one ranged GET per span        awscrt (optional; EXPERIMENTAL, untested here)
#
# A mechanism signature is mech(reader, s3url, spans, timings) -> list[bytes]. `timings`, if a list, collects per-GET
# seconds where the mechanism can measure them (the thread-pool and async-gather ones can; the single batched
# cat_ranges call cannot and leaves it empty). Size mismatches raise IOError so a short/garbage read never passes.


def _split_s3(url: str) -> tuple[str, str]:
    p = url[5:] if url.startswith("s3://") else url
    bucket, _, key = p.partition("/")
    return bucket, key


def _check_span_sizes(blobs, spans) -> list[bytes]:
    out = list(blobs)
    if len(out) != len(spans):
        raise IOError(f"s3 range fetch returned {len(out)} blobs for {len(spans)} spans")
    for b, (off, size) in zip(out, spans):
        if isinstance(b, BaseException):
            raise IOError(f"s3 range GET failed at offset {off}: {b!r}")
        if len(b) != size:
            raise IOError(f"s3 range GET short: got {len(b)} of {size} bytes at offset {off}")
    return out


def _mech_s3fs(reader: "RangeReader", url: str, spans, timings=None) -> list[bytes]:
    """Baseline: one cat_file per span in a thread pool (byte-for-byte the historical in-region path)."""
    if not spans:
        return []

    def one(s):
        off, size = s
        if timings is None:
            return reader._get_s3(url, off, size)
        t = time.perf_counter()
        b = reader._get_s3(url, off, size)
        timings.append(time.perf_counter() - t)
        return b

    with ThreadPoolExecutor(reader.threads) as ex:
        return list(ex.map(one, spans))


def _mech_s3fs_ranges(reader: "RangeReader", url: str, spans, timings=None) -> list[bytes]:
    """fsspec/s3fs cat_ranges: hand the whole span list to one batched, internally-async call. max_gap=None so it
    fetches exactly the spans we pass (we already coalesced). Per-GET latency is not observable (one batched call)."""
    if not spans:
        return []
    fs = reader._s3fs()
    paths = [url] * len(spans)
    starts = [off for off, _ in spans]
    ends = [off + size for off, size in spans]
    out = reader._s3_with_refresh(lambda: fs.cat_ranges(paths, starts, ends, max_gap=None, on_error="raise"))
    return _check_span_sizes(out, spans)


def _mech_aiobotocore(reader: "RangeReader", url: str, spans, timings=None) -> list[bytes]:
    """Async get_object coroutines gathered on one persistent event loop — no thread pool, pure async concurrency,
    bounded to reader.threads for an apples-to-apples comparison with the thread-pool mechanisms."""
    if not spans:
        return []
    bucket, key = _split_s3(url)
    out = reader._s3_with_refresh(
        lambda: reader._aio_client().get_ranges(bucket, key, spans, reader.threads, timings))
    return _check_span_sizes(out, spans)


def _mech_boto3(reader: "RangeReader", url: str, spans, timings=None) -> list[bytes]:
    """Sync boto3 get_object per span in a thread pool. Only importable if boto3 is installed (it may not be)."""
    if not spans:
        return []
    bucket, key = _split_s3(url)

    def one(s):
        off, size = s
        t = time.perf_counter()
        resp = reader._boto3_client().get_object(Bucket=bucket, Key=key, Range=f"bytes={off}-{off + size - 1}")
        data = resp["Body"].read()
        if timings is not None:
            timings.append(time.perf_counter() - t)
        return data

    def run():
        with ThreadPoolExecutor(reader.threads) as ex:
            return list(ex.map(one, spans))

    return _check_span_sizes(reader._s3_with_refresh(run), spans)


def _mech_crt(reader: "RangeReader", url: str, spans, timings=None) -> list[bytes]:
    """awscrt S3 client, one signed ranged GET per span. EXPERIMENTAL: awscrt is not installed in this environment,
    so this path is untested — the benchmark isolates any failure and reports it rather than trusting its timings."""
    if not spans:
        return []
    bucket, key = _split_s3(url)

    def one(s):
        off, size = s
        t = time.perf_counter()
        data = reader._crt_client().get_range(bucket, key, off, size)
        if timings is not None:
            timings.append(time.perf_counter() - t)
        return data

    def run():
        with ThreadPoolExecutor(reader.threads) as ex:
            return list(ex.map(one, spans))

    return _check_span_sizes(reader._s3_with_refresh(run), spans)


class _AioS3:
    """A cached aiobotocore S3 client living on a dedicated background event loop. One per RangeReader: the client is
    entered once and reused across every granule/rep, so the async path is not re-charged client setup on each fetch."""

    def __init__(self, creds: dict, region: str = S3_REGION, max_pool: int = 32):
        import asyncio
        self._creds = creds
        self.region = region
        self.max_pool = max_pool
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="aicesat-aio", daemon=True)
        self._thread.start()
        self._client = None
        self._client_cm = None

    def _run(self):
        import asyncio
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _submit(self, coro):
        import asyncio
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    async def _ensure_client(self):
        if self._client is None:
            import aiobotocore.session
            from botocore.config import Config
            sess = aiobotocore.session.AioSession()
            self._client_cm = sess.create_client(
                "s3", region_name=self.region,
                aws_access_key_id=self._creds["accessKeyId"],
                aws_secret_access_key=self._creds["secretAccessKey"],
                aws_session_token=self._creds["sessionToken"],
                config=Config(max_pool_connections=self.max_pool, retries={"max_attempts": 3}))
            self._client = await self._client_cm.__aenter__()
        return self._client

    async def _get_ranges(self, bucket, key, spans, concurrency, timings):
        import asyncio
        client = await self._ensure_client()
        sem = asyncio.Semaphore(concurrency)

        async def one(off, size):
            async with sem:
                t = time.perf_counter()
                resp = await client.get_object(Bucket=bucket, Key=key, Range=f"bytes={off}-{off + size - 1}")
                async with resp["Body"] as body:
                    data = await body.read()
                if timings is not None:
                    timings.append(time.perf_counter() - t)
                return data

        return await asyncio.gather(*(one(off, size) for off, size in spans))

    def get_ranges(self, bucket, key, spans, concurrency, timings=None) -> list[bytes]:
        return self._submit(self._get_ranges(bucket, key, spans, concurrency, timings))

    def close(self):
        if self._client_cm is not None:
            try:
                self._submit(self._client_cm.__aexit__(None, None, None))
            except Exception:
                pass
            self._client_cm = self._client = None
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception:
            pass


class _CrtS3:
    """awscrt S3 client wrapper (EXPERIMENTAL, untested). S3RequestType.DEFAULT passes each request through the CRT
    signer as a single ranged GET (no CRT-managed multipart), which is what a small-range workload wants."""

    def __init__(self, creds: dict, region: str = S3_REGION):
        from awscrt import auth, io, s3
        self.region = region
        self._elg = io.EventLoopGroup(1)
        resolver = io.DefaultHostResolver(self._elg)
        bootstrap = io.ClientBootstrap(self._elg, resolver)
        provider = auth.AwsCredentialsProvider.new_static(
            access_key_id=creds["accessKeyId"], secret_access_key=creds["secretAccessKey"],
            session_token=creds["sessionToken"])
        self._client = s3.S3Client(bootstrap=bootstrap, region=region, credential_provider=provider,
                                   throughput_target_gbps=10.0)

    def get_range(self, bucket: str, key: str, off: int, size: int) -> bytes:
        from awscrt import http, s3
        chunks: list[bytes] = []
        headers = http.HttpHeaders([("Host", f"{bucket}.s3.{self.region}.amazonaws.com"),
                                    ("Range", f"bytes={off}-{off + size - 1}")])
        request = http.HttpRequest("GET", f"/{key}", headers)
        req = self._client.make_request(type=s3.S3RequestType.DEFAULT, request=request,
                                        on_body=lambda chunk, **kw: chunks.append(bytes(chunk)))
        req.finished_future.result()   # raises on non-2xx / transport error
        return b"".join(chunks)

    def close(self):
        self._client = None


S3_FETCH_MECHANISMS = {
    "s3fs": _mech_s3fs,               # default == historical path; keep FIRST so it is the obvious baseline
    "s3fs_ranges": _mech_s3fs_ranges,
    "aiobotocore": _mech_aiobotocore,
    "boto3": _mech_boto3,
    "crt": _mech_crt,
}


def s3_mechanism_available(name: str) -> tuple[bool, str]:
    """(importable?, reason-if-not) for a mechanism, so the benchmark runs whatever subset the box has installed.
    Uses find_spec (no heavy import just to probe)."""
    import importlib.util
    reqs = {"s3fs": ["s3fs"], "s3fs_ranges": ["s3fs"], "aiobotocore": ["aiobotocore", "botocore"],
            "boto3": ["boto3"], "crt": ["awscrt"]}
    mods = reqs.get(name)
    if mods is None:
        return False, f"unknown mechanism {name!r}"
    missing = [m for m in mods if importlib.util.find_spec(m) is None]
    return (False, "missing " + ", ".join(missing)) if missing else (True, "")


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
