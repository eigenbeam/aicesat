# Task: make the MCP server deployable on AWS in us-west-2

**Status:** done — deployed 2026-08-28 with the kit in `deploy/` (`1a3f9cd`); runbook, updating and by-hand
operation are in `deploy/README.md`. Issue #5 closed 2026-09-24. The plan below is kept as written on 2026-08-25;
where the build departed from it:

- **Remote access: both paths, one box.** The owner runs the stdio MCP over SSH (ungated; the SSH key is the auth).
  A public beta web app sits behind Caddy, gated by a shared code (`AICESAT_PUBLIC_URL` / `AICESAT_ACCESS_CODE`,
  `server.py`). This resolves the first open question below.
- **Data plane:** region-gated S3-direct byte-range reads in `access.py` (`in_region`, `s3_credentials`,
  `access_url`; s3fs by default, `AICESAT_S3_FETCH` selects others), checked by `deploy/verify_region.py`
  (presigns must be 0). HTTPS/CloudFront remains the out-of-region path. Index, coverage and lake live on the
  instance's EBS volume (`AICESAT_DATA_DIR=/opt/aicesat/data`), not on S3.
- **Packaging:** no container and no Secrets Manager — `deploy/bootstrap.sh` + systemd (`aicesat-web.service`) +
  Caddy on a single t3.large, configured from `aicesat.env` (template: `deploy/aicesat.env.example`).

## Why (measured)
From a remote laptop over WiFi the access path is latency-bound, not bandwidth-bound: 104–157 ms TTFB per range GET
(CloudFront edge → S3 us-west-2 origin), 1.7–2.0 s per EDL presign, ~40 MB/s link ceiling, 22–30 MB/s aggregate at
8–16 streams. In-region (NSIDC spike, t3.large): ~10–30 ms per GET, S3 direct with STS credentials (no CloudFront hop,
no egress charges), per-connection 50–100 MB/s scaling to the NIC. Expected effect on our workload (8 granules, EGIG
box): cold touch 74 s → ~15–20 s with no code changes; index build 47 s → ~10–15 s; whole-granule paths 9 min → ~1 min.
SlideRule's 13 s wall-clock is location, not code.

## What "deployable" means here
- The stdio MCP server (`aicesat-server`) becomes reachable from Claude Desktop **remotely**: run it with the
  `streamable-http` transport behind an authenticated endpoint (mcp 2.x supports `mcp.run(transport="streamable-http")`),
  or keep stdio and bridge via SSH. Decide: HTTP transport + a bearer/OAuth guard vs SSH tunnel (simplest for one user).
- Data plane in-region: `access.py` gains an S3 path (`earthaccess.get_s3_credentials()` → boto3/s3fs range GETs on
  `s3://nsidc-cumulus-prod-protected/...`, credentials refreshed hourly), selected automatically when running in
  us-west-2; HTTPS/CloudFront stays the out-of-region fallback. Index files, coverage DuckDB and the Parquet lake move
  to S3 (DuckDB `httpfs`/`read_parquet('s3://…')` with hive partitioning) or an EBS volume; `AICESAT_DATA_DIR` already
  abstracts the root.
- The widget server (localhost:8765 today) is served from the same instance behind the same auth, or the widget is
  published as static files with the API behind it; imagery mosaics and scene JSON are generated in-region.
- Packaging: container image (uv-based), `EARTHDATA_TOKEN` from Secrets Manager, region check at startup, one
  instance (c5n/m6i class with ≥10 Gbit) — this is a single-user tool, not a service (spec §0).
- The benchmark (`scripts/bench_access.py`) reruns in-region to fill the "us-west-2" column that is currently an
  estimate; keep the laptop column so the comparison stays honest about where the wins come from.

## Open questions
- ~~SSH-bridged stdio vs HTTP transport for Claude Desktop~~ — resolved: Claude Desktop uses SSH-bridged stdio. The
  public path is a gated web app, not an HTTP MCP transport; see Status.
- Keep the lake in-region only, or sync a per-region subset back to the laptop for offline demos.
- Offline index pre-build as a scheduled job (all ATL03 granules over Greenland per season) — pairs naturally with
  running in-region.

## Related
- Spec §11 open item: "SourceAdapter fetch mechanism — S3 direct + auth".
- `docs/plans/atl24-on-demand-reads.md` in nsidc/open-altimetry (private) for their in-region measurements.
