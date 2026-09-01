"""Offline unit tests for the GLAS + ICESSN sub-granule indexers (pure logic; end-to-end byte-identity is validated
against live NASA data separately)."""
import numpy as np

from aicesat import index_glas, index_icessn, api


# ---- ICESSN line-offset index: span union + CSV field parsing --------------------------------------------------
def test_merge_unions_overlapping_and_adjacent_spans():
    # overlapping (10-30 & 20-40 -> 10-40), touching (40-50 -> 10-50), disjoint (60-70 stays separate)
    assert index_icessn._merge([(20, 40), (10, 30), (40, 50), (60, 70)]) == [(10, 50), (60, 70)]
    assert index_icessn._merge([]) == []
    assert index_icessn._merge([(5, 9)]) == [(5, 9)]


def test_merge_keeps_every_line_once_no_overlap():
    merged = index_icessn._merge([(0, 100), (50, 150), (150, 200)])
    assert merged == [(0, 200)]                                   # fully coalesced, no byte covered twice


def test_parse_fields_nadir_line_and_lon_normalization():
    # seconds,lat,lon(0..360),elev,sn,we,rms,npt,npt_edit,dist,track
    ln = b"50000.0,70.0,315.0,2600.5,0.1,0.2,12.0,40,0,100,0"
    lat, lon, elev, rms, track, sn, we = index_icessn._parse_fields(ln)
    assert lat == 70.0 and abs(lon - (-45.0)) < 1e-9 and elev == 2600.5 and rms == 12.0 and track == 0
    assert sn == 0.1 and we == 0.2                           # ILATM2 platelet plane-fit slopes (S->N, W->E)


def test_parse_span_points_carries_platelet_slopes():
    # two nadir lines (track==0, RMS < 50 cm) with distinct S-N / W-E plane-fit slopes -> both kept, slopes preserved
    blob = (b"50000.0,70.0,315.0,2600.5,0.10,-0.20,12.0,40,0,100,0\n"
            b"50001.0,70.001,315.001,2601.0,0.05,0.15,10.0,40,0,100,0")
    pts = index_icessn._parse_span_points([blob], "20120315", index_icessn.ICESSN_RES)
    assert pts["lon"].size == 2
    assert np.allclose(pts["sn_slope"], [0.10, 0.05])
    assert np.allclose(pts["we_slope"], [-0.20, 0.15])


def test_parse_fields_rejects_comment_short_and_fill():
    assert index_icessn._parse_fields(b"# header line") is None
    assert index_icessn._parse_fields(b"") is None
    assert index_icessn._parse_fields(b"1,2,3") is None                       # too few columns
    assert index_icessn._parse_fields(b"5e4,****,315.0,2600,0,0,9,4,0,1,0") is None  # '****' fill in lat


# ---- GLAS fill handling ----------------------------------------------------------------------------------------
def test_nan_fill_replaces_fill_value():
    a = np.array([1.0, 3.4e38, 2.0], dtype="f8")
    out = index_glas._nan_fill(a, 3.4e38)
    assert np.isnan(out[1]) and out[0] == 1.0 and out[2] == 2.0


def test_nan_fill_no_fill_is_identity():
    a = np.array([1.0, 2.0], dtype="f4")
    out = index_glas._nan_fill(a, float("nan"))          # nan fill -> nothing matches -> unchanged values
    assert np.array_equal(out, a.astype("f8"))


# ---- collection -> index wiring --------------------------------------------------------------------------------
def test_index_source_maps_all_missions():
    for coll in ("ATL06", "ICESAT2", "ATL03", "GLAS", "ICESSN"):
        d, res = api._index_source(coll)
        assert d is not None and isinstance(res, int), coll
    assert api._index_source("NOPE") == (None, None)


# ---- in-region S3-direct gating (pure; the S3 read path itself is validated in us-west-2) -----------------------
def test_in_region_env_gating(monkeypatch):
    from aicesat import access
    for v in ("AICESAT_S3_DIRECT", "AWS_REGION", "AWS_DEFAULT_REGION"):
        monkeypatch.delenv(v, raising=False)
    assert access.in_region() is False
    monkeypatch.setenv("AWS_REGION", "us-west-2"); assert access.in_region() is True
    monkeypatch.setenv("AWS_REGION", "us-east-1"); assert access.in_region() is False
    monkeypatch.setenv("AICESAT_S3_DIRECT", "1"); assert access.in_region() is True     # explicit override wins
    monkeypatch.setenv("AICESAT_S3_DIRECT", "0"); assert access.in_region() is False


def test_access_url_prefers_s3_only_in_region(monkeypatch):
    from aicesat import access
    monkeypatch.setenv("AICESAT_S3_DIRECT", "0")
    assert access.access_url("https://cf/x", "s3://b/x") == "https://cf/x"
    monkeypatch.setenv("AICESAT_S3_DIRECT", "1")
    assert access.access_url("https://cf/x", "s3://b/x") == "s3://b/x"
    assert access.access_url("https://cf/x", None) == "https://cf/x"    # no s3 link -> HTTPS even in-region
    assert access.access_url("https://cf/x", "") == "https://cf/x"


def test_aio_client_is_shared_per_process_not_per_reader(monkeypatch):
    """A RangeReader is built PER GRANULE, so the aiobotocore client must not be.

    _AioS3 starts a background event-loop thread holding an aiobotocore client, and building that client makes
    botocore parse its multi-megabyte S3 service model. Nothing called RangeReader.close(), so a per-reader client
    left a live thread pinning that JSON forever: ~19 MB per granule, which OOM-killed the ATL06 Greenland build at
    3,090 of 32,608 granules. Readers sharing a token must share one client.
    """
    from aicesat import access

    built = []

    class _FakeAio:
        def __init__(self, creds, region=None, max_pool=None):
            built.append((creds.get("sessionToken"), region, max_pool))
            self.closed = False

        def close(self):
            self.closed = True

    monkeypatch.setattr(access, "_AioS3", _FakeAio)
    monkeypatch.setattr(access, "_AIO_CACHE", {})
    creds = {"accessKeyId": "A", "secretAccessKey": "S", "sessionToken": "TOK-1"}
    monkeypatch.setattr(access, "s3_credentials", lambda refresh=False: creds)

    readers = [access.RangeReader.__new__(access.RangeReader) for _ in range(5)]
    for r in readers:
        r.threads, r._aio = 8, None
    clients = [r._aio_client() for r in readers]

    assert len(built) == 1, f"one client per process, got {len(built)} — the per-granule leak is back"
    assert all(c is clients[0] for c in clients), "every reader must get the same shared client"

    # close() must NOT stop a client other readers are still using.
    readers[0].close()
    assert clients[0].closed is False, "closing one reader killed the shared background loop"
    assert readers[1]._aio_client() is clients[0], "the shared client was dropped from the cache"

    # A refreshed STS token retires the old client rather than stacking one up per hourly refresh.
    creds2 = {"accessKeyId": "A", "secretAccessKey": "S", "sessionToken": "TOK-2"}
    monkeypatch.setattr(access, "s3_credentials", lambda refresh=False: creds2)
    readers[2]._aio = None
    fresh = readers[2]._aio_client()
    assert fresh is not clients[0] and clients[0].closed is True, "the expired-token client was neither replaced nor closed"
    assert len(access._AIO_CACHE) == 1, "clients accumulate across token refreshes"
