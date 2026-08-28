"""In-region smoke test — run ON the us-west-2 box after bootstrap. Confirms the S3-direct read path we could not
exercise from a laptop actually works: region detection, STS credentials, and an S3-direct index fetch, with timing.

  cd /opt/aicesat && uv run python deploy/verify_region.py

Green here means the whole reason for deploying to us-west-2 is delivering. Compare `presigns` (should be 0 in-region)
and the wall-clock against the laptop numbers in the session notes.
"""
import time

from aicesat import auth, access


def main():
    auth.login()
    print(f"1. in_region(): {access.in_region()}   (must be True — else set AWS_REGION=us-west-2)")
    assert access.in_region(), "not in-region: S3-direct will be skipped; check AWS_REGION"

    t = time.time()
    c = access.s3_credentials()
    print(f"2. STS credentials: got keys {sorted(c)[:3]}... in {time.time()-t:.1f}s")

    # A tiny known-indexed box (Jakobshavn) — GLAS + ICESSN + ATL06 should all be S3-direct here.
    box = [-50.3, 68.9, -49.2, 69.3]
    from aicesat import index_glas, index_icessn, index_atl06
    for name, mod, res in [("GLAS", index_glas, index_glas.GLAS_RES),
                           ("ICESSN", index_icessn, index_icessn.ICESSN_RES),
                           ("ATL06", index_atl06, index_atl06.ATL06_RES)]:
        d = mod._index_dir(res)
        if not d.exists() or not any(d.glob("*.parquet")):
            print(f"3. {name}: no index built here yet — skip (build with scripts/build_{name.lower()}_index.py)")
            continue
        t = time.time()
        arr, st = mod.fetch_bbox(box, window=None, res=res)
        dt = time.time() - t
        print(f"3. {name}: {arr['h'].size} pts, {dt:.1f}s | GETs {st.get('requests')}, "
              f"presigns {st.get('presigns')} (0 = S3-direct working), {st.get('bytes',0)/1e6:.1f} MB")
    print("\nOK — if presigns are 0 and the fetches returned points, S3-direct is live.")


if __name__ == "__main__":
    main()
