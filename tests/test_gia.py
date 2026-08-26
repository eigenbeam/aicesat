"""GIA uplift-rate sampling and the co-registration GIA Δh shift. Offline — uses the vendored ICE-6G_C grid."""
from aicesat import coreg, gia  # noqa: E401


def test_uplift_rate_sign_and_convention():
    # Positive = uplift: Hudson Bay and Fennoscandia are strong post-glacial rebound maxima.
    assert gia.uplift_rate_mm_yr(-95.0, 60.0) > 5.0        # Hudson Bay
    assert gia.uplift_rate_mm_yr(20.0, 63.0) > 5.0         # Fennoscandia
    # Amazon is far from any ice load -> near zero.
    assert abs(float(gia.uplift_rate_mm_yr(-60.0, 0.0))) < 1.0
    # Longitude convention: -95 and 265 are the same meridian.
    assert abs(float(gia.uplift_rate_mm_yr(-95.0, 60.0)) - float(gia.uplift_rate_mm_yr(265.0, 60.0))) < 1e-6
    assert gia.MODEL == "ICE-6G_C (VM5a)" and "10.1002/2014JB011176" in gia.CITATION


def test_gia_block_shift_sign():
    # dh_shift = -u*(t_IS2 - t_GLAS): over uplifting bedrock, the later mission sees a higher surface, so removing
    # GIA lowers Δh(IS2-GLAS) -> negative shift; over subsiding bedrock -> positive.
    up = {"bbox": [-96, 59, -94, 61]}     # Hudson Bay: strong uplift (u > 0)
    b = coreg._gia_block(up, t_is2=2020.0, t_glas=2005.0)
    assert b is not None and b["uplift_rate_mm_per_yr"] > 5.0
    assert b["years_apart_signed"] == 15.0
    assert b["dh_shift_m"] < 0 and abs(b["dh_shift_m"] - (-b["uplift_rate_mm_per_yr"] / 1000 * 15.0)) < 1e-3
    assert b["unresolved_key"] == "GIA" and "10.1002/2014JB011176" in b["citation"]


def test_gia_block_none_off_grid_is_graceful():
    # A malformed doc (no bbox) must not raise — the block just comes back None.
    assert coreg._gia_block({}, 2020.0, 2005.0) is None
