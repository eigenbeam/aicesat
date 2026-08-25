import numpy as np
from aicesat import atl03


def test_strong_beams():
    assert atl03.strong_beams(0) == ["gt1l", "gt2l", "gt3l"]
    assert atl03.strong_beams(1) == ["gt1r", "gt2r", "gt3r"]
    assert atl03.strong_beams(2) == []


def test_land_ice_column():
    assert atl03.LAND_ICE_COL == 3


def test_delta_time_to_utc():
    # ATLAS SDP epoch = 2018-01-01T00:00:00 UTC = GPS 1198800018 s (18 leap seconds)
    t = atl03.delta_time_to_utc(np.array([0.0, 86400.0]), 1198800018.0)
    assert str(t[0]) == "2018-01-01T00:00:00.000"
    assert str(t[1]) == "2018-01-02T00:00:00.000"
