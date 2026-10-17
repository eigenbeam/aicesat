"""Tier-1 read primitives (spec §6.2–6.3): presigned-URL resolution, concurrent HTTPS byte-range GETs, chunk decode.

Nothing here opens or parses an HDF5 file. Byte ranges and filter pipelines come from the index (index.py),
decode is zlib (stdlib) + the HDF5 byte-shuffle transpose in numpy, validated byte-identical against h5py
(scripts/probe_range_get.py). Every request and byte is counted for the access scoreboard.
"""
from __future__ import annotations

import logging
import io
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
    from_buffer: int = 0         # build-time: chunk ranges sliced from a whole-object buffer instead of fetched

    def as_dict(self) -> dict:
        return {"requests": self.requests, "bytes": self.bytes, "seconds": round(self.seconds, 2), "chunks": self.chunks,
                "spans": self.spans, "gap_bytes": self.gap_bytes,
                "presigns": self.presigns, "granules_touched": len(self.granules_touched),
                "hdf5_opens_at_query_time": self.hdf5_opens, "structure_parses_at_query_time": self.structure_parses}


# Coalescing gap: merge two wanted ranges into one GET if the unwanted gap between them is smaller than this.
#
# The break-even gap is (round-trip latency) x (bandwidth AVAILABLE TO ONE CONNECTION) — not the whole link. That
# distinction is the whole ballgame: the fetch runs FETCH_WORKER_CAP connections at once, so each gets roughly
# link/workers. This constant was 2 MB out-of-region, justified as "100-160 ms TTFB, ~40 MB/s, so requests under
# ~6 MB are latency-bound" — which divided by no workers at all and overestimated the optimum by about the worker
# count. Measured from a laptop (scripts/bench_coalesce.py, 50 granules, 45 MB wanted, 16 workers):
#
#      gap    spans   MB read   best s   ms/GET
#     0.06    1,090        47      2.8     40.5
#     0.12      999        55      2.8     45.5   <- the knee
#     0.25      728       109      5.6    122.1
#     1.00      248       344     14.4    929.8
#     2.00      237       362     15.4   1040.1   <- the old default, 5.5x slower than the knee
#
# ms/GET is flat below ~128 KB and climbs steeply above it, and read throughput is ~17-24 MB/s throughout, so past
# the knee every extra byte of over-fetch is paid for at link speed and buys nothing. In-region keeps 1 MB: egress is
# free, RTT is 10-30 ms and per-connection bandwidth is far higher, which moves the knee right (see bench_coalesce).
# Override with AICESAT_COALESCE_GAP (bytes).
MAX_GAP_BYTES = int(os.environ.get("AICESAT_COALESCE_GAP", 1 << 17))
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


def map_as_completed(fn, items: list, workers: int, on_each=None) -> list:
    """`fn` over `items` on `workers` threads; returns the results in `items` order. `on_each(result)`, when given, is
    called on the CALLER's thread as each item finishes, in completion order -- so a build streams each granule as it
    lands instead of every granule after the slowest. (fetch_bbox used `list(ex.map(...))` and then looped: ICESat-2
    reached the page in one burst at the end of its fetch.) A failure propagates, as ex.map's did."""
    if workers <= 1:
        out = []
        for it in items:
            r = fn(it)
            out.append(r)
            if on_each is not None:
                on_each(r)
        return out
    from concurrent.futures import as_completed
    out = [None] * len(items)
    with ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(fn, it): i for i, it in enumerate(items)}
        for f in as_completed(futs):
            r = f.result()
            out[futs[f]] = r
            if on_each is not None:
                on_each(r)
    return out


# Per-granule fetch fan-out (fetch_bbox across granules, in index_{atl06,glas,icessn}). Each granule's own fetch
# already uses RangeReader's internal concurrency (`threads`), so outer x inner requests are in flight at once. The
# out-of-region urllib3 pool is sized to that product (it discards overflow connections, see __init__); the in-region
# aiobotocore pool is not, because measurement showed it makes no difference (see aio_pool_size). Env
# AICESAT_FETCH_WORKERS overrides the outer width.
FETCH_WORKER_CAP = 16       # network-latency-bound, not CPU-bound — see pool_size(cpu_bound=False)
FETCH_MIN_GRANULES = 3          # 1-2 granules: fetch serially (a pool would only add overhead)
FETCH_WORKER_ENV = "AICESAT_FETCH_WORKERS"
S3_POOL_ENV = "AICESAT_S3_POOL"  # in-region: aiobotocore connection pool size override (experiments; see aio_pool_size)


def aio_pool_size(threads: int) -> int:
    """Connection pool size for the shared in-region aiobotocore client: max(threads, 32), or AICESAT_S3_POOL.

    The fetch fans out as outer granules x inner `threads` requests against ONE shared client, so 16 x 16 = 256 GETs
    can be in flight over a 32-connection pool, and the flat ~0.35-0.45 s per GET regardless of size looked like
    queueing. Measured (scripts/bench_coalesce.py, Summit 320 granules / 10,205 ranges / 361 MB wanted, best of 3):

        pool  workers   gap 0.5 MiB   1 MiB   2 MiB
          32       16        6.4 s    6.3 s   6.2 s
         256       16        6.3 s    6.5 s   6.6 s
          32        8        6.0 s    6.3 s   6.2 s
         128        8        6.1 s    6.2 s   6.5 s

    Identical within noise. Halving the workers halves ms/GET (35.6 -> 16.7) at the same wall: a shared cap of
    ~300-400 MB/s read, not pool queueing. The cap turned out to be THIS CLIENT'S single Python event-loop thread
    (100% of one core during a fetch, the other three idle; two processes together reached 520 MB/s), not the
    instance: the CRT mechanism moves the bytes to C and reads 670-840 MB/s on the same box (see _CrtS3). The pool
    default stays at the floor; the env override exists to re-run this on other hardware."""
    override = _env_int(S3_POOL_ENV)
    if override is not None:
        return override
    return max(threads, 32)


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
    return (1 << 20) if (in_region() if reg is None else reg) else (1 << 17)   # 1 MB in-region, 128 KB out (measured)


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


_AIO_CACHE: dict = {}
_AIO_LOCK = threading.Lock()


def _aio_shared(creds: dict, region: str, max_pool: int) -> "_AioS3":
    """One _AioS3 per (STS token, region, pool size) per PROCESS.

    _AioS3 starts a background event-loop thread and enters an aiobotocore S3 client on it, and building that client
    makes botocore parse its multi-megabyte S3 service model. Its docstring says "one per RangeReader ... reused
    across every granule" — but every caller builds a RangeReader PER GRANULE, and none of them called close(), so
    each granule left a live thread pinning a client and its parsed JSON. Measured growth was ~19 MB per granule with
    no plateau; eight pool workers reached ~4.8 GB each and the kernel OOM-killed the ATL06 Greenland build at 3,090
    of 32,608 granules (/proc/vmstat oom_kill, anon-rss 4,891,488 kB). Caching by token fixes every caller at once,
    the server's per-scene readers included, and bounds the process to one loop thread per distinct credential.
    """
    key = (creds.get("sessionToken"), region, max_pool)
    with _AIO_LOCK:
        got = _AIO_CACHE.get(key)
        if got is None:
            for k, old in list(_AIO_CACHE.items()):     # a new token retires the old clients rather than stacking
                if k[0] != key[0]:                      # one up per hourly STS refresh
                    _AIO_CACHE.pop(k, None)
                    try:
                        old.close()
                    except Exception:
                        pass
            got = _AIO_CACHE[key] = _AioS3(creds, region=region, max_pool=max_pool)
        return got


def _aio_evict() -> None:
    """Close and forget every shared client — used when STS creds are found expired mid-flight."""
    with _AIO_LOCK:
        for _, obj in list(_AIO_CACHE.items()):
            try:
                obj.close()
            except Exception:
                pass
        _AIO_CACHE.clear()


def access_url(https_url: str, s3_url: str | None) -> str:
    """The URL to byte-range at run time: S3-direct in-region (no presign, no egress), else HTTPS/CloudFront.
    The index stores both, so a query picks the fast path for wherever it runs."""
    return s3_url if (in_region() and s3_url) else https_url


class RangeReader:
    """Concurrent byte-range reader over Earthdata Cloud granules. Out-of-region: bearer -> 303 -> presigned CloudFront
    HTTPS GETs. In-region (s3:// URLs): direct range GETs against nsidc-cumulus S3 with STS creds — no presign, no
    egress — via a selectable mechanism (env AICESAT_S3_FETCH; default "crt", see S3_FETCH_MECHANISMS). Wanted ranges
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
        #
        # CONFIRMED in a second, 90x smaller regime (bench_vs_h5coro, 10 granules / 6.7 MB wanted), where the worry
        # was that 1 MB over-fetches 6.3x and would lose to a tighter gap. It does not:
        #     256 KiB  83 reqs  13.3 MB  0.85 s      1 MiB  25 reqs  42.3 MB  0.53 s
        #     512 KiB  52 reqs  24.9 MB  0.61 s      2 MiB  25 reqs  42.3 MB  0.69 s
        # 3.2x more bytes buys a 1.6x speedup. The 2 MiB row reads IDENTICAL spans and bytes to 1 MiB — coalescing has
        # saturated, every bridgeable gap already bridged — so the 0.53 vs 0.69 difference is pure noise, which puts
        # the median's noise floor near +/-0.16 s and makes 512 KiB and 1 MiB indistinguishable here. Only 256 KiB is
        # genuinely worse. Do not re-litigate this for "small queries"; that is the case already measured.
        self.max_gap = max_gap if max_gap is not None else default_coalesce_gap(reg)
        auth.login()
        self.token = os.environ["EARTHDATA_TOKEN"]
        self.session = requests.Session()
        # NSIDC/CloudFront return transient 5xx on a small fraction of range GETs; requests' default retries none of them.
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        retry = Retry(total=5, backoff_factor=0.6, status_forcelist=(429, 500, 502, 503, 504),
                      allowed_methods=frozenset({"GET"}), respect_retry_after_header=True)
        self.threads = threads if threads is not None else (16 if reg else 8)   # S3 scales; in-region go wider
        # Size the pool to the concurrency this session ACTUALLY sees. fetch_bbox builds ONE reader and hands it to
        # an outer pool of up to FETCH_WORKER_CAP granules, each of which fetches its spans with `threads` inside —
        # so 16 x 8 = 128 out-of-region, not the 32 the old comment assumed. urllib3 does not queue past pool_maxsize:
        # it opens the connection, uses it once and discards it ("Connection pool is full, discarding connection",
        # 24 of them in one scene build), so every overflow request pays a fresh TLS handshake against CloudFront at
        # 100-160 ms TTFB. This does not widen concurrency — the requests were already being made — it stops the
        # churn of throwing their connections away.
        outer = _env_int(FETCH_WORKER_ENV) or FETCH_WORKER_CAP
        pool = max(32, outer * self.threads)
        self.session.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=pool, pool_maxsize=pool))
        self.stats = AccessStats()
        self._presigned: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()
        self._url_locks: dict[str, threading.Lock] = {}  # one lock per granule URL: presigns for different granules overlap
        self._s3 = None                                  # lazily-built s3fs filesystem (in-region S3-direct)
        # Pluggable in-region S3 range-GET mechanism (see S3_FETCH_MECHANISMS); "s3fs" is the historical path.
        # Flip the winner in one place: export AICESAT_S3_FETCH=<name>. Default: "crt" when awscrt is installed
        # (2.2x faster than aiobotocore on the Summit fetch, see _CrtS3), else aiobotocore.
        # aiobotocore won the earlier in-region benchmark on both shapes (~26% faster on many-small-GETs, tighter p95 than
        # the s3fs cat_file thread pool), byte-identical. Override with AICESAT_S3_FETCH (s3fs|s3fs_ranges|boto3|crt).
        self.s3_fetch = os.environ.get("AICESAT_S3_FETCH") or default_s3_mechanism()
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
        name = getattr(self, "s3_fetch", None) or default_s3_mechanism()
        mech = S3_FETCH_MECHANISMS.get(name)
        if mech is None:
            raise ValueError(f"unknown AICESAT_S3_FETCH={name!r}; choose from {sorted(S3_FETCH_MECHANISMS)}")
        return mech(self, url, spans, timings)

    def _s3_reset(self):
        """Drop every cached S3 client so the next call rebuilds with fresh STS creds."""
        self._s3 = None
        _aio_evict()          # the shared clients hold the expired token too; a per-reader reset alone would not help
        _crt_evict()
        self._aio = self._crt = None
        for attr in ("_boto3",):
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
            self._aio = _aio_shared(s3_credentials(), S3_REGION, aio_pool_size(self.threads))
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
            self._crt = _crt_shared(s3_credentials(), S3_REGION)
        return self._crt

    def close(self):
        """Release this reader's own resources. Safe to call more than once.

        Neither the aiobotocore nor the CRT client is closed here: both are shared process-wide (_aio_shared,
        _crt_shared), and closing one would stop loops other readers are still using. Dropping the references is
        enough; the caches own their lifetimes."""
        self._crt = self._aio = None

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
            # In-region S3-direct: the mechanism (AICESAT_S3_FETCH, default crt) owns its own concurrency and
            # cred self-heal; every mechanism returns the same bytes.
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
# AICESAT_S3_FETCH (default "crt" when awscrt is installed, else "aiobotocore"; "s3fs" is the historical path). Adding/removing a mechanism is a
# one-line edit of S3_FETCH_MECHANISMS below; flipping the production default is one env var (or one line in __init__).
#
#   name          concurrency model                              deps (beyond the always-present s3fs)
#   ------------  ---------------------------------------------  -------------------------------------
#   s3fs          cat_file per span in a ThreadPoolExecutor       (the historical baseline)
#   s3fs_ranges   fsspec cat_ranges (batched, async internally)   —
#   aiobotocore   async get_object gathered on one event loop     aiobotocore + botocore (installed)
#   boto3         sync get_object in a ThreadPoolExecutor          boto3 (optional; may be absent)
#   crt           awscrt S3 client, one ranged GET per span        awscrt (C event loops; the default where installed)
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
    """awscrt S3 client, one signed ranged GET per span, all of a granule's spans submitted at once: the CRT owns the
    concurrency (its connection pool, its event-loop threads), so there is no Python thread per request to add."""
    if not spans:
        return []
    bucket, key = _split_s3(url)
    out = reader._s3_with_refresh(lambda: reader._crt_client().get_ranges(bucket, key, spans, timings))
    return _check_span_sizes(out, spans)


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


_CRT_CACHE: dict = {}
CRT_GBPS_ENV = "AICESAT_CRT_GBPS"


def _crt_shared(creds: dict, region: str) -> "_CrtS3":
    """One CRT S3 client per (STS token, region) per process, for the same reason as _aio_shared: readers are built
    per granule, and a CRT client owns event-loop threads and a connection pool that must not be rebuilt 320 times."""
    key = (creds.get("sessionToken"), region)
    with _AIO_LOCK:
        got = _CRT_CACHE.get(key)
        if got is None:
            for k in list(_CRT_CACHE):
                if k[0] != key[0]:
                    _CRT_CACHE.pop(k).close()
            got = _CRT_CACHE[key] = _CrtS3(creds, region)
        return got


def _crt_evict() -> None:
    with _AIO_LOCK:
        for obj in list(_CRT_CACHE.values()):
            try:
                obj.close()
            except Exception:
                pass
        _CRT_CACHE.clear()


class _CrtS3:
    """awscrt S3 client wrapper. S3RequestType.DEFAULT passes each request through the CRT signer as a single ranged
    GET (no CRT-managed multipart), which is what a small-range workload wants.

    Why it is the default: the aiobotocore path moved every byte through ONE Python event-loop thread, and that thread
    sat at 100% CPU while the other cores idled (top -H during a fetch; two concurrent processes only reached 520 MB/s
    together). The CRT does TLS, HTTP and body handling in C on one event loop per core. Measured on the box
    (scripts/bench_coalesce.py, Summit 320 granules / 10,205 ranges / 361 MB wanted, best of 3, byte-identical output):

        mechanism / setting        gap 0.5 MiB   1 MiB   2 MiB
        aiobotocore                   6.4 s      6.3 s   6.2 s
        crt, target 10 Gbps           3.8 s      3.7 s   6.0 s (first row of the run pays connection warm-up)
        crt, target 100 Gbps          2.8 s      2.9 s   3.3 s   (670-840 MB/s read)

    `throughput_target_gbps` is how the CRT sizes its connection pool, not a promise about the NIC: at 10 the pool was
    the limit (requests queued), at 100 it is not. AICESAT_CRT_GBPS overrides it. The coalescing knee also moved: with
    the CRT a 0.5 MiB gap is as fast as 1 MiB while reading 700 MB less."""

    def __init__(self, creds: dict, region: str = S3_REGION):
        from awscrt import auth, io, s3
        self.region = region
        self._elg = io.EventLoopGroup(0)        # 0: one loop thread per core
        resolver = io.DefaultHostResolver(self._elg)
        bootstrap = io.ClientBootstrap(self._elg, resolver)
        provider = auth.AwsCredentialsProvider.new_static(
            access_key_id=creds["accessKeyId"], secret_access_key=creds["secretAccessKey"],
            session_token=creds["sessionToken"])
        self._client = s3.S3Client(bootstrap=bootstrap, region=region, credential_provider=provider,
                                   throughput_target_gbps=float(_env_int(CRT_GBPS_ENV) or 100))

    def _start(self, bucket: str, key: str, off: int, size: int, want_headers: bool = False):
        """Issue one ranged GET; returns (future, chunks, state). state collects the S3 error body so an expired-token
        403 surfaces as text _s3_with_refresh can recognise (the CRT's own exception only names the status). With
        `want_headers` the response headers land in state["headers"] (read_whole_object reads Content-Range)."""
        from awscrt import http, s3
        chunks: list[bytes] = []
        state: dict = {}
        headers = http.HttpHeaders([("Host", f"{bucket}.s3.{self.region}.amazonaws.com"),
                                    ("Range", f"bytes={off}-{off + size - 1}")])
        request = http.HttpRequest("GET", f"/{key}", headers)

        def on_done(error=None, error_headers=None, error_body=None, status_code=None, **_kw):
            state.update(error=error, status=status_code, body=error_body)

        extra = {}
        if want_headers:
            def on_headers(status_code=None, headers=None, **_kw):
                state.update(status=status_code, headers=list(headers or []))
            extra["on_headers"] = on_headers
        req = self._client.make_request(type=s3.S3RequestType.DEFAULT, operation_name="GetObject", request=request,
                                        on_body=lambda chunk, **kw: chunks.append(bytes(chunk)), on_done=on_done, **extra)
        return req, chunks, state

    def get_range_with_headers(self, bucket: str, key: str, off: int, size: int) -> tuple[bytes, dict]:
        """One ranged GET and its response headers, lower-cased: Content-Range carries the object's total size."""
        req, chunks, state = self._start(bucket, key, off, size, want_headers=True)
        try:
            req.finished_future.result()
        except Exception as e:
            body = (state.get("body") or b"").decode("utf-8", "replace") if isinstance(state.get("body"), (bytes, bytearray)) else str(state.get("body") or "")
            raise IOError(f"S3 range GET failed (status {state.get('status')}): {e} {body[:300]}") from e
        return b"".join(chunks), {str(k).lower(): str(v) for k, v in state.get("headers") or []}

    def get_ranges(self, bucket: str, key: str, spans, timings=None) -> list[bytes]:
        t0 = time.perf_counter()
        started = [self._start(bucket, key, off, size) for off, size in spans]
        out = []
        for req, chunks, state in started:
            try:
                req.finished_future.result()
            except Exception as e:
                body = (state.get("body") or b"").decode("utf-8", "replace") if isinstance(state.get("body"), (bytes, bytearray)) else str(state.get("body") or "")
                raise IOError(f"S3 range GET failed (status {state.get('status')}): {e} {body[:300]}") from e
            if timings is not None:
                timings.append(time.perf_counter() - t0)
            out.append(b"".join(chunks))
        return out

    def get_range(self, bucket: str, key: str, off: int, size: int) -> bytes:
        return self.get_ranges(bucket, key, [(off, size)])[0]

    def close(self):
        self._client = None


def default_s3_mechanism() -> str:
    """The in-region mechanism used when AICESAT_S3_FETCH is unset: crt where awscrt imports, else aiobotocore."""
    return "crt" if s3_mechanism_available("crt")[0] else "aiobotocore"


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


# --- whole-object reads for small granules at build time -------------------------------------------------------------
# h5py walks a granule's structure (superblock, groups, object headers, chunk B-trees) through the fsspec block cache:
# ~66 serial 1 MiB reads per ATL06 granule at ~31 ms each, 2.0 s of a 3.3 s build (scripts/bench_build_io.py,
# 2026-10-17). An ATL06 granule is 32-128 MB; read whole through the CRT in 8 MiB spans it takes 0.2-0.25 s, and h5py
# over the in-memory bytes walks the same structure in milliseconds, then the position chunks are SLICED from the same
# buffer instead of fetched. A builder opts in per collection (ATL03 never does: 0.2-6.7 GB); the cap below decides per
# object, and out-of-region nothing changes.
WHOLE_OBJECT_MAX_ENV = "AICESAT_WHOLE_OBJECT_MAX_BYTES"
WHOLE_OBJECT_MAX_BYTES = 256 << 20      # per worker the peak is 2-3x the object (join + BytesIO); 16 workers x 128 MB fits
WHOLE_OBJECT_SPAN = 8 << 20


def whole_object_max_bytes() -> int:
    """The size cap for a whole-object read; 0 disables it (the bench's --whole-object off)."""
    v = os.environ.get(WHOLE_OBJECT_MAX_ENV)
    if v is None or v == "":
        return WHOLE_OBJECT_MAX_BYTES
    try:
        return max(0, int(v))
    except ValueError:
        return WHOLE_OBJECT_MAX_BYTES


def slice_ranges(buf, ranges) -> list[bytes]:
    """The wanted (offset, size) slices of one in-memory object, in order; IOError if a range runs off the end."""
    n = len(buf)
    out = []
    for off, size in ranges:
        if off < 0 or off + size > n:
            raise IOError(f"range ({off}, {size}) runs off the end of a {n}-byte object")
        out.append(bytes(buf[off:off + size]))
    return out


def _content_range_total(headers: dict) -> int | None:
    cr = headers.get("content-range", "")
    if "/" not in cr:
        return None
    total = cr.rsplit("/", 1)[1].strip()
    return int(total) if total.isdigit() else None


def read_whole_object(reader: "RangeReader", s3url: str, max_bytes: int | None = None) -> bytes | None:
    """The whole object behind an s3:// URL through the CRT, or None when it is larger than the cap.

    The first 8 MiB span goes out alone and the object's size is read from its Content-Range -- no HEAD, no extra
    round trip -- then the remaining spans go out in one submission. Over the cap, that first span is the only cost
    and the caller falls back to the block cache. Counted on reader.stats like any fetch."""
    cap = whole_object_max_bytes() if max_bytes is None else max_bytes
    if cap <= 0:
        return None
    bucket, key = _split_s3(s3url)
    t0 = time.time()

    def go():
        crt = reader._crt_client()
        first, hdrs = crt.get_range_with_headers(bucket, key, 0, WHOLE_OBJECT_SPAN)
        total = _content_range_total(hdrs)
        if total is None and len(first) < WHOLE_OBJECT_SPAN:
            total = len(first)                      # the whole object fit in the first span
        if total is None:
            raise IOError("no Content-Range on the first span; cannot size the object")
        if total > cap:
            return None, 1, len(first)
        spans = [(o, min(WHOLE_OBJECT_SPAN, total - o)) for o in range(WHOLE_OBJECT_SPAN, total, WHOLE_OBJECT_SPAN)]
        rest = _check_span_sizes(crt.get_ranges(bucket, key, spans), spans) if spans else []
        buf = b"".join([first[:total], *rest])
        if len(buf) != total:
            raise IOError(f"whole-object read short: {len(buf)} of {total} bytes")
        return buf, 1 + len(spans), total

    try:
        buf, n_req, n_bytes = reader._s3_with_refresh(go)
    except IOError as e:
        log.warning("whole-object read of %s failed (%s); falling back to the block cache", s3url, e)
        return None
    with reader._lock:
        reader.stats.requests += n_req
        reader.stats.spans += n_req
        reader.stats.bytes += n_bytes
        reader.stats.seconds += time.time() - t0
        reader.stats.granules_touched.add(s3url)
    return buf


class GranuleSource:
    """One granule for a builder: the object h5py opens, and ranged reads that come from the SAME bytes.

    With a whole-object buffer, `open()` is an in-memory file and `fetch()` slices; without one, `open()` is
    cloud_hdf5_file (the block cache) and `fetch()` goes through a RangeReader, built lazily if none was given."""

    def __init__(self, url: str, s3url: str | None = None, reader: "RangeReader | None" = None, buf: bytes | None = None):
        self.url, self.s3url, self.reader, self.buf = url, s3url or "", reader, buf
        self._stats = AccessStats()

    @property
    def whole_object(self) -> bool:
        return self.buf is not None

    @property
    def stats(self) -> AccessStats:
        return self.reader.stats if self.reader is not None else self._stats

    def open(self):
        if self.buf is not None:
            return io.BytesIO(self.buf)
        return cloud_hdf5_file(self.url, self.s3url or None, reader=self.reader)

    def fetch(self, ranges: list[tuple[int, int]]) -> list[bytes]:
        if self.buf is not None:
            self.stats.from_buffer += len(ranges)
            return slice_ranges(self.buf, ranges)
        if self.reader is None:
            self.reader = RangeReader()
        return self.reader.fetch(access_url(self.url, self.s3url or None), ranges)


def open_granule(url: str, s3url: str | None = None, *, whole_object: bool = False, max_bytes: int | None = None,
                 reader: "RangeReader | None" = None) -> GranuleSource:
    """A GranuleSource for a builder. `whole_object` is honoured only in-region, with an s3:// URL, through the CRT,
    and under the size cap; everywhere else this is exactly the block-cache path of before.

    Measured (scripts/bench_build_io.py, m6i.xlarge, 2026-10-17):

        ATL06, 100 granules (32-128 MB)      block cache   whole object
          8 workers                            44.7 s        21.3 s      (CPU 46% -> 94%)
         16 workers                            31.5 s        25.0 s
        ATL06, 300 granules, whole object: 6 / 8 / 12 workers -> 5.36 / 5.51 / 5.29 granules/s, CPU 92-98%
        GLAS,  35 granules (2-5 MB), 8 workers  7.4 s         4.1 s

    The build stops being latency-bound and becomes CPU-bound at ~0.7 core-seconds per ATL06 granule on this box
    (TLS + copy of ~88 MB, the H3 assignment, zlib), so the worker count no longer matters past the core count and a
    full ATL06 rebuild is ~1.7 h instead of 3.9 h. More cores are the next lever, not more workers."""
    cap = whole_object_max_bytes() if max_bytes is None else max_bytes
    buf = None
    if whole_object and s3url and cap > 0 and in_region():
        r = reader if reader is not None else RangeReader()
        if (getattr(r, "s3_fetch", None) or default_s3_mechanism()) == "crt":
            buf = read_whole_object(r, s3url, cap)
            reader = r
    return GranuleSource(url, s3url, reader=reader, buf=buf)


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
