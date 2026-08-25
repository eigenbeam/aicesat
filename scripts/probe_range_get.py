"""Access primitive check: HTTPS byte-range GET with the EDL bearer token vs h5py's own chunk bytes; decode incl. shuffle."""
import os, sys, time, zlib, requests, earthaccess, h5py, numpy as np
from aicesat import auth, coverage, regions
auth.login(); tok = os.environ["EARTHDATA_TOKEN"]
bbox = regions.resolve_bbox(regions.DEFAULT_REGION)
g = coverage.search("ATL03", "007", bbox, regions.DEFAULT_ATL03_WINDOW)[:1][0]
url = g.data_links()[0]
f = h5py.File(earthaccess.open([g], show_progress=False)[0], "r")

def unshuffle(buf: bytes, itemsize: int) -> bytes:
    a = np.frombuffer(buf, dtype="u1"); n = a.size // itemsize
    return a[: n * itemsize].reshape(itemsize, n).T.copy().tobytes()

s = requests.Session()
for name, k in [("gt1r/heights/h_ph", 5), ("gt1r/heights/signal_conf_ph", 5), ("gt1r/heights/lat_ph", 120)]:
    d = f[name]; ci = d.id.get_chunk_info(k)
    fm, ref_raw = d.id.read_direct_chunk(ci.chunk_offset)
    t0 = time.time()
    r = s.get(url, headers={"Authorization": f"Bearer {tok}", "Range": f"bytes={ci.byte_offset}-{ci.byte_offset + ci.size - 1}"}, allow_redirects=True, timeout=60)
    dt = time.time() - t0
    hist = [(h.status_code, h.headers.get("Location", "")[:60]) for h in r.history]
    print(f"{name}[{k}]: status={r.status_code} bytes={len(r.content)}/{ci.size} in {dt:.2f}s; redirects={hist}; final host={r.url.split('/')[2]}")
    same_raw = r.content == ref_raw
    buf = zlib.decompress(r.content)
    if d.shuffle:
        buf = unshuffle(buf, d.dtype.itemsize)
    arr = np.frombuffer(buf, dtype=d.dtype).reshape((-1,) + d.chunks[1:])
    sl = tuple(slice(o, o + c) for o, c in zip(ci.chunk_offset, d.chunks))
    ref = d[sl]
    print(f"   raw bytes identical={same_raw}; decoded == h5py read: {np.array_equal(arr[: ref.shape[0]], ref)} (shuffle={d.shuffle}, dtype={d.dtype}, shape={arr.shape})")
# reuse of the presigned URL for many ranges (avoid re-auth per chunk) + concurrency
final = r.url
d = f["gt1r/heights/lat_ph"]; infos = [d.id.get_chunk_info(k) for k in range(100, 116)]
from concurrent.futures import ThreadPoolExecutor
def fetch(ci):
    rr = s.get(final, headers={"Range": f"bytes={ci.byte_offset}-{ci.byte_offset + ci.size - 1}"}, timeout=60); return rr.status_code, len(rr.content)
t0 = time.time(); res = list(ThreadPoolExecutor(8).map(fetch, infos)); dt = time.time() - t0
print(f"16 chunk GETs on the presigned URL, 8 threads: {dt:.2f}s, {sum(b for _, b in res)/1e6:.1f} MB, statuses {sorted(set(c for c, _ in res))}")
