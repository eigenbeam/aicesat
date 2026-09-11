"""ATL06 timestamps are UTC, and agree with the ATL03 path to the millisecond.

`atlas_sdp_gps_epoch` is GPS seconds, so the SDP epoch reached by adding it to the GPS epoch sits 18 s ahead of the
epoch's true UTC instant. index_atl06 used to skip that subtraction while the ATL03 path (planner/atl03) applied it,
so the same overpass carried two dates 18 s apart depending on which product you asked for.
"""
import numpy as np

from aicesat import planner
from aicesat.atl03 import delta_time_to_utc
from aicesat.index_atl06 import _atlas_epoch_years

SDP_GPS = 1198800018.0     # ancillary_data/atlas_sdp_gps_epoch, i.e. 2018-01-01T00:00:00Z in GPS seconds


def test_sdp_epoch_maps_to_the_utc_instant_the_user_guide_names():
    assert _atlas_epoch_years(np.array([0.0]), SDP_GPS)[0] == np.datetime64("2018-01-01T00:00:00.000", "ms")


def test_atl06_and_atl03_date_the_same_delta_time_identically():
    dt = np.array([0.0, 1.0, 12345.678, 2.5e7])
    assert np.array_equal(_atlas_epoch_years(dt, SDP_GPS), delta_time_to_utc(dt, SDP_GPS))


def test_atl06_agrees_with_the_atl03_lake_writer_epoch():
    """planner materialises ATL03 photons with its own GPS_EPOCH_MS constant; the two must not drift apart."""
    dt = np.array([0.0, 999.25])
    theirs = planner.GPS_EPOCH_MS + ((dt + SDP_GPS) * 1000).astype("timedelta64[ms]")
    assert np.array_equal(_atlas_epoch_years(dt, SDP_GPS), theirs)
