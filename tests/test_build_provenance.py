"""A granule file's footer must record the build's FILTER set, not the cells the granule happened to touch.

Two builders shadowed their `cells` parameter (the filter) with the per-point cell array and then stamped that array
into the footer. The consequences differ by direction and the ATL03 one is the dangerous one:

  * ATL03 recorded the UNFILTERED cells of the last beam. A file that dropped every row outside the filter could
    therefore PROVE ground it did not hold, so an enlarging build skipped it and the added ring was never indexed —
    the silent-short-scene class that per-granule provenance exists to prevent.
  * GLAS recorded touched ∩ filter: never wrongly True, but one sidecar per granule (4,220 for 4,799 files on the
    box) and a spurious rebuild of every granule on an enlarging build.

Both tests build a synthetic granule whose track crosses two cells, X and Y, with the filter set to {X}, and assert the
recorded set is {X}: it proves X and cannot prove Y.
"""
import h3
import h5py
import numpy as np
import pytest

from aicesat import access, auth, index, index_glas

LAT0, LON0 = 72.50, -38.50


class _Granule(dict):
    def __init__(self, name):
        super().__init__()
        self["meta"] = {"native-id": name, "revision-id": 1}
        self._url = f"https://example.invalid/{name}"

    def data_links(self, access=None):
        return [] if access == "direct" else [self._url]


class _LocalReader:
    """RangeReader over a local file: the builders' position fetch sliced from disk instead of S3."""
    def __init__(self, path):
        self._data = path.read_bytes()
        self.stats = access.AccessStats()

    def fetch(self, url, ranges):
        return [self._data[o:o + s] for o, s in ranges]


def _two_cells(res):
    """A short north-going track and the two res-`res` cells its ends fall in."""
    lat = np.linspace(LAT0, LAT0 + 0.25, 400)
    lon = np.full(400, LON0)
    x = h3.str_to_int(h3.latlng_to_cell(float(lat[0]), LON0, res))
    y = h3.str_to_int(h3.latlng_to_cell(float(lat[-1]), LON0, res))
    assert x != y, "the synthetic track must cross a cell boundary"
    return lat, lon, x, y


def _synthetic_atl03(path, lat, lon):
    """One beam of an ATL03 granule: 400 segments of 20 photons, heights chunked at 1,000 photons."""
    n_seg, per = lat.size, 20
    n_ph = n_seg * per
    with h5py.File(path, "w") as f:
        f.create_dataset("orbit_info/sc_orient", data=np.array([0], "i1"))
        f.create_dataset("ancillary_data/atlas_sdp_gps_epoch", data=np.array([1.198800018e9], "f8"))
        geo = "gt1l/geolocation"
        for name, arr in (("reference_photon_lat", lat), ("reference_photon_lon", lon),
                          ("ph_index_beg", np.arange(1, n_ph + 1, per, dtype="i8")),
                          ("segment_ph_cnt", np.full(n_seg, per, "i4"))):
            f.create_dataset(f"{geo}/{name}", data=arr, chunks=(100,), compression="gzip")
        h = "gt1l/heights"
        f.create_dataset(f"{h}/lat_ph", data=np.repeat(lat, per), chunks=(1000,), compression="gzip")
        f.create_dataset(f"{h}/lon_ph", data=np.repeat(lon, per), chunks=(1000,), compression="gzip")
        f.create_dataset(f"{h}/h_ph", data=np.full(n_ph, 2000.0, "f4"), chunks=(1000,), compression="gzip")
        f.create_dataset(f"{h}/delta_time", data=np.linspace(0, 10, n_ph), chunks=(1000,), compression="gzip")
        f.create_dataset(f"{h}/signal_conf_ph", data=np.zeros((n_ph, 5), "i1"), chunks=(1000, 5), compression="gzip")
    return path


def _synthetic_glah06(path, lat, lon):
    n = lat.size
    vals = {"lat": lat, "lon": lon % 360.0, "elev": np.full(n, 2000.0), "sat_corr": np.zeros(n), "delta_ellip": np.zeros(n),
            "time": np.linspace(2.5e8, 2.5e8 + n, n), "elev_use": np.zeros(n, "i1"), "sat_flag": np.zeros(n, "i1")}
    with h5py.File(path, "w") as f:
        for key, dpath in index_glas.GLAS_DATASETS:
            f.create_dataset(dpath, data=vals[key], chunks=(100,), compression="gzip")
    return path


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr(auth, "login", lambda *a, **k: None)
    monkeypatch.setattr(access, "in_region", lambda: False)


def _check(path, x, y):
    recorded = index.granule_cells(path)
    assert recorded == {h3.int_to_str(x)}, f"the footer must record the FILTER set {{X}}, got {recorded}"
    assert index.granule_proves(path, [x])
    assert not index.granule_proves(path, [y]), "a file built for X alone must not prove Y"


def test_atl03_footer_records_the_filter_set_not_the_touched_cells(tmp_path, monkeypatch):
    lat, lon, x, y = _two_cells(index.H3_RES)
    h5 = _synthetic_atl03(tmp_path / "ATL03_20200312172457_11760603_007_01.h5", lat, lon)
    monkeypatch.setattr(index, "ATL03_INDEX_DIR", tmp_path / "idx")
    monkeypatch.setattr(access, "RangeReader", lambda *a, **k: _LocalReader(h5))
    monkeypatch.setattr(access, "cloud_hdf5_file", lambda url, s3=None, **k: str(h5))
    tbl = index.build_granule_index(_Granule(h5.name), cells=[x])
    assert tbl.num_rows > 0 and set(tbl.column("h3_cell").to_pylist()) == {x}, "rows are filtered to X"
    _check(tmp_path / "idx" / f"{h5.name}.parquet", x, y)


def test_glas_footer_records_the_filter_set_not_the_touched_cells(tmp_path, monkeypatch):
    """GLAS recorded touched ∩ filter. With the filter {X, Y, Z} and a track through X and Y only, the file must still
    record all three and prove Z, or an enlarging build over Z rebuilds a granule that already covers it."""
    lat, lon, x, y = _two_cells(index_glas.GLAS_RES)
    z = h3.str_to_int(h3.latlng_to_cell(-75.0, 0.0, index_glas.GLAS_RES))        # far away: never touched
    h5 = _synthetic_glah06(tmp_path / "GLAH06_634_2131_002_0045_1_01_0001.H5", lat, lon)
    monkeypatch.setattr(index_glas, "GLAS_INDEX_DIR", tmp_path / "idx")
    monkeypatch.setattr(access, "open_granule", lambda url, s3=None, **k: access.GranuleSource(url, s3, buf=h5.read_bytes()))
    tbl = index_glas.build_glas_index(_Granule(h5.name), cells=[x, y, z])
    assert set(tbl.column("h3_cell").to_pylist()) == {x, y}
    path = tmp_path / "idx" / "res5" / f"{h5.name}.parquet"
    assert index.granule_cells(path) == set(index.compacted_cellset([x, y, z]))
    assert index.granule_proves(path, [z]), "the filter covered Z, so the file proves Z even though no shot fell there"
    assert len(list((tmp_path / "idx" / "res5" / index.CELLSETS_DIR).glob("*.json"))) == 1
