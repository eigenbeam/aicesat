# Task: make the MCP server deployable on AWS in us-west-2

**Status:** saved for later (2026-08-25). Not started.

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
- SSH-bridged stdio vs HTTP transport for Claude Desktop (auth story, latency of a chatty stdio protocol over SSH).
- Keep the lake in-region only, or sync a per-region subset back to the laptop for offline demos.
- Offline index pre-build as a scheduled job (all ATL03 granules over Greenland per season) — pairs naturally with
  running in-region.

## Related
- Spec §11 open item: "SourceAdapter fetch mechanism — S3 direct + auth".
- `docs/plans/atl24-on-demand-reads.md` in nsidc/open-altimetry (private) for their in-region measurements.
