"""Index-build groundwork: ATL03's real HDF5 chunk layout, filters, and h5py chunk-info throughput over HTTPS."""
import sys, time, json, earthaccess, h5py, numpy as np
from aicesat import auth, coverage, regions
auth.login()
bbox = regions.resolve_bbox(sys.argv[1] if len(sys.argv) > 1 else regions.DEFAULT_REGION)
g = coverage.search("ATL03", "007", bbox, regions.DEFAULT_ATL03_WINDOW)[:1][0]
print("granule", g["meta"]["native-id"], "size MB", round(g.size(), 1))
print("links", [l for l in g.data_links(access="direct")][:2], [l for l in g.data_links()][:2])
t0 = time.time(); fobj = earthaccess.open([g], show_progress=False)[0]; f = h5py.File(fobj, "r"); print("open %.1fs" % (time.time() - t0))
for name in ["gt1r/heights/lat_ph", "gt1r/heights/lon_ph", "gt1r/heights/h_ph", "gt1r/heights/signal_conf_ph", "gt1r/heights/delta_time",
             "gt1r/geolocation/ph_index_beg", "gt1r/geolocation/segment_ph_cnt", "gt1r/geolocation/reference_photon_lat"]:
    d = f[name]
    dcpl = d.id.get_create_plist()
    filters = [dcpl.get_filter(i) for i in range(dcpl.get_nfilters())]
    n = d.id.get_num_chunks() if d.chunks else 0
    print(f"{name}: shape={d.shape} dtype={d.dtype} chunks={d.chunks} compression={d.compression}({d.compression_opts}) shuffle={d.shuffle} "
          f"scaleoffset={d.scaleoffset} fletcher32={d.fletcher32} nchunks={n} filters={[(fl[0], fl[2]) for fl in filters]}")
    if n:
        t0 = time.time()
        infos = [d.id.get_chunk_info(i) for i in range(min(n, 3))]
        print("   first chunks:", [(ci.chunk_offset, ci.byte_offset, ci.size, ci.filter_mask) for ci in infos], "(%.2fs for 3)" % (time.time() - t0))
# throughput of enumerating every chunk of one big dataset
d = f["gt1r/heights/h_ph"]; n = d.id.get_num_chunks()
t0 = time.time(); sizes = [d.id.get_chunk_info(i).size for i in range(n)]; dt = time.time() - t0
print(f"enumerated {n} chunks of h_ph in {dt:.2f}s ({n/dt:.0f} chunks/s); total compressed {sum(sizes)/1e6:.1f} MB, mean chunk {np.mean(sizes)/1e3:.1f} kB")
# does h5py's chunk iterator exist and is it faster?
if hasattr(d.id, "chunk_iter"):
    t0 = time.time(); k = 0
    d.id.chunk_iter(lambda ci: None); dt2 = time.time() - t0
    print(f"chunk_iter over {n} chunks: {dt2:.2f}s")
# read one raw chunk directly and decode with zlib (+shuffle) to check byte-identical decode
ci = d.id.get_chunk_info(5)
fm, raw = d.id.read_direct_chunk(ci.chunk_offset)
import zlib
buf = zlib.decompress(raw)
arr = np.frombuffer(buf, dtype=d.dtype)
if d.shuffle:  # HDF5 shuffle: byte-transpose; undo it
    es = d.dtype.itemsize; nel = len(buf) // es
    arr = np.frombuffer(np.frombuffer(buf, dtype="u1").reshape(es, nel).T.copy().tobytes(), dtype=d.dtype)
ref = d[ci.chunk_offset[0]: ci.chunk_offset[0] + d.chunks[0]]
print("direct-chunk decode byte-identical to h5py read:", np.array_equal(arr[:ref.size], ref), "chunk elems", arr.size, "raw bytes", len(raw))
