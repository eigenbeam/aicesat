# Catalogue search vs the sub-granule index, measured 2026-10-19

Produced by `uv run python scripts/measure_search_modes.py --out /tmp/search-modes.md` on the deployed box (m6i.xlarge, us-west-2), 2026-10-19 02:50 UTC, against the indexes listed below (ATL03 is the pre-cutover v6-less index; the staged v6 index has the same granules). Langtang rows are absent because no index claim covers it; GEDI has no index on the box.
Column meanings are in the script's docstring. `gran_mb_quartiles` are Q1/median/Q3 of the CMR granule size.

Index builds measured against:

- ATL03: 48,838 granule files, target 48838, window ['2018-10-01', '2027-01-01'], at None
- ATL06: 33,484 granule files, target 33484, window None, at None
- GEDI: no index
- GLAS: 4,799 granule files, target 4799, window None, at None
- ICESSN: 33,811 granule files, target 33811, window None, at None

| area | collection | cmr_hits | cmr_gb | cmr_hull | idx_cells | idx_box | false_pos | missed | need_mb | bytes_ratio | fp_area | fp_kind | chunks_per_gran | gran_mb_quartiles |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| summit | ATL06 | 549 | 45.0 | 726 | 656 | 602 | 0.096 | 0 | 646.9 | 69.6 | 8.1 | polygon | 4 | 72.0/80.0/96.0 |
| summit | ATL03 | 602 | 1,197.0 | 666 | 664 | 592 | 0.003 | 0 | 12,754.1 | 93.9 | 9.8 | polygon | 91.5 | 966.0/1,344.0/2,656.0 |
| summit | GLAS | 120 | 0.45 | 156 | 137 | 137 | 0.147 | 4 | 69.7 | 6.4 | – | orbit | 1 | 3.3/3.9/4.2 |
| summit | ICESSN | 195 | 0.03 | 271 | 251 | 193 | 0.074 | 0 | 25.2 | 1.3 | 0 | polygon | – | 0.1/0.1/0.1 |
| egig_west_flank | ATL06 | 553 | 48.6 | 761 | 665 | 605 | 0.126 | 0 | 654.5 | 74.3 | 8.1 | polygon | 5 | 72.0/96.0/104.0 |
| egig_west_flank | ATL03 | 560 | 1,040.3 | 656 | 600 | 556 | 0.085 | 0 | 12,071.9 | 86.2 | 8.5 | polygon | 104.0 | 968.0/1,456.0/2,560.0 |
| egig_west_flank | GLAS | 139 | 0.52 | 139 | 127 | 127 | 0.086 | 0 | 64.8 | 8 | – | orbit | 1 | 3.4/3.7/4.2 |
| egig_west_flank | ICESSN | 89 | 0.02 | 233 | 208 | 93 | 0.107 | 0 | 13.2 | 1.7 | 0 | polygon | – | 0.1/0.1/0.5 |
| k_transect_upstream | ATL06 | 557 | 49.9 | 649 | 579 | 566 | 0.111 | 2 | 625.5 | 79.7 | 7.2 | polygon | 5 | 80.0/96.0/104.0 |
| k_transect_upstream | ATL03 | 567 | 1,102.5 | 598 | 543 | 517 | 0.092 | 0 | 11,737.7 | 93.9 | 7.5 | polygon | 115 | 1,024.0/1,472.0/2,680.0 |
| k_transect_upstream | GLAS | 108 | 0.4 | 140 | 102 | 102 | 0.271 | 0 | 49.1 | 8.2 | – | orbit | 1 | 3.4/3.8/4.2 |
| k_transect_upstream | ICESSN | 321 | 0.08 | 556 | 513 | 311 | 0.077 | 0 | 56.7 | 1.4 | 0 | polygon | – | 0.1/0.1/0.5 |
| jakobshavn_margin | ATL06 | 571 | 47.1 | 749 | 644 | 626 | 0.14 | 0 | 751.7 | 62.6 | 7.2 | polygon | 5 | 64.0/88.0/96.0 |
| jakobshavn_margin | ATL03 | 576 | 1,410.4 | 637 | 608 | 574 | 0.046 | 0 | 20,080.6 | 70.2 | 8.2 | polygon | 109.0 | 954.0/1,400.0/3,518.0 |
| jakobshavn_margin | GLAS | 93 | 0.35 | 138 | 113 | 113 | 0.181 | 0 | 55.7 | 6.3 | – | orbit | 1 | 3.3/3.9/4.3 |
| jakobshavn_margin | ICESSN | 504 | 0.14 | 882 | 775 | 492 | 0.121 | 0 | 101.9 | 1.3 | 0 | polygon | – | 0.1/0.1/0.5 |
| summit_traverse | ATL06 | 109 | 9.02 | 220 | 150 | 140 | 0.318 | 0 | 104.9 | 85.9 | 852.3 | polygon | 4 | 72.0/88.0/96.0 |
| summit_traverse | ATL03 | 116 | 234.4 | 175 | 144 | 96 | 0.177 | 0 | 493.2 | 475.4 | 1,009.3 | polygon | 84.5 | 920.0/1,404.0/2,762.0 |
| summit_traverse | GLAS | 15 | 0.06 | 31 | 21 | 21 | 0.323 | 0 | 11.2 | 5.3 | – | orbit | 1 | 3.6/4/4.5 |
| summit_traverse | ICESSN | 43 | 0.01 | 63 | 62 | 43 | 0.016 | 0 | 4.09 | 2.3 | 0.3 | polygon | – | 0.1/0.1/0.5 |

```json
[
 {
  "area": "summit",
  "collection": "ATL06",
  "window": [
   "2018-10-01",
   "2027-01-01"
  ],
  "bbox": [
   -39.5,
   72.4,
   -37.5,
   72.8
  ],
  "query_km2": 2980.4,
  "cmr_hits": 549,
  "cmr_gb": 45.05,
  "cmr_hull": 726,
  "idx_cells": 656,
  "idx_box": 602,
  "false_pos": 0.096,
  "missed": 0,
  "need_mb": 646.938,
  "bytes_ratio": 69.6,
  "fp_area": 8.1,
  "fp_kind": "polygon",
  "chunks_per_gran": 4.0,
  "gran_mb_quartiles": [
   72.0,
   80.0,
   96.0
  ],
  "index_rows": 14340
 },
 {
  "area": "summit",
  "collection": "ATL03",
  "window": [
   "2018-10-01",
   "2027-01-01"
  ],
  "bbox": [
   -39.5,
   72.4,
   -37.5,
   72.8
  ],
  "query_km2": 2980.4,
  "cmr_hits": 602,
  "cmr_gb": 1197.0,
  "cmr_hull": 666,
  "idx_cells": 664,
  "idx_box": 592,
  "false_pos": 0.003,
  "missed": 0,
  "need_mb": 12754.131,
  "bytes_ratio": 93.9,
  "fp_area": 9.8,
  "fp_kind": "polygon",
  "chunks_per_gran": 91.5,
  "gran_mb_quartiles": [
   966.0,
   1344.0,
   2656.0
  ],
  "index_rows": 39367
 },
 {
  "area": "summit",
  "collection": "GLAS",
  "window": [
   "2003-02-20",
   "2009-10-11"
  ],
  "bbox": [
   -39.5,
   72.4,
   -37.5,
   72.8
  ],
  "query_km2": 2980.4,
  "cmr_hits": 120,
  "cmr_gb": 0.45,
  "cmr_hull": 156,
  "idx_cells": 137,
  "idx_box": 137,
  "false_pos": 0.147,
  "missed": 4,
  "need_mb": 69.748,
  "bytes_ratio": 6.4,
  "fp_area": null,
  "fp_kind": "orbit",
  "chunks_per_gran": 1,
  "gran_mb_quartiles": [
   3.3,
   3.9,
   4.2
  ],
  "index_rows": 523
 },
 {
  "area": "summit",
  "collection": "ICESSN",
  "window": [
   "2009-01-01",
   "2019-12-31"
  ],
  "bbox": [
   -39.5,
   72.4,
   -37.5,
   72.8
  ],
  "query_km2": 2980.4,
  "cmr_hits": 195,
  "cmr_gb": 0.03,
  "cmr_hull": 271,
  "idx_cells": 251,
  "idx_box": 193,
  "false_pos": 0.074,
  "missed": 0,
  "need_mb": 25.168,
  "bytes_ratio": 1.3,
  "fp_area": 0.0,
  "fp_kind": "polygon",
  "chunks_per_gran": null,
  "gran_mb_quartiles": [
   0.1,
   0.1,
   0.1
  ],
  "index_rows": 428
 },
 {
  "area": "egig_west_flank",
  "collection": "ATL06",
  "window": [
   "2018-10-01",
   "2027-01-01"
  ],
  "bbox": [
   -45.0,
   69.8,
   -43.0,
   70.2
  ],
  "query_km2": 3407.5,
  "cmr_hits": 553,
  "cmr_gb": 48.6,
  "cmr_hull": 761,
  "idx_cells": 665,
  "idx_box": 605,
  "false_pos": 0.126,
  "missed": 0,
  "need_mb": 654.544,
  "bytes_ratio": 74.3,
  "fp_area": 8.1,
  "fp_kind": "polygon",
  "chunks_per_gran": 5,
  "gran_mb_quartiles": [
   72.0,
   96.0,
   104.0
  ],
  "index_rows": 13799
 },
 {
  "area": "egig_west_flank",
  "collection": "ATL03",
  "window": [
   "2018-10-01",
   "2027-01-01"
  ],
  "bbox": [
   -45.0,
   69.8,
   -43.0,
   70.2
  ],
  "query_km2": 3407.5,
  "cmr_hits": 560,
  "cmr_gb": 1040.34,
  "cmr_hull": 656,
  "idx_cells": 600,
  "idx_box": 556,
  "false_pos": 0.085,
  "missed": 0,
  "need_mb": 12071.871,
  "bytes_ratio": 86.2,
  "fp_area": 8.5,
  "fp_kind": "polygon",
  "chunks_per_gran": 104.0,
  "gran_mb_quartiles": [
   968.0,
   1456.0,
   2560.0
  ],
  "index_rows": 38446
 },
 {
  "area": "egig_west_flank",
  "collection": "GLAS",
  "window": [
   "2003-02-20",
   "2009-10-11"
  ],
  "bbox": [
   -45.0,
   69.8,
   -43.0,
   70.2
  ],
  "query_km2": 3407.5,
  "cmr_hits": 139,
  "cmr_gb": 0.52,
  "cmr_hull": 139,
  "idx_cells": 127,
  "idx_box": 127,
  "false_pos": 0.086,
  "missed": 0,
  "need_mb": 64.815,
  "bytes_ratio": 8.0,
  "fp_area": null,
  "fp_kind": "orbit",
  "chunks_per_gran": 1,
  "gran_mb_quartiles": [
   3.4,
   3.7,
   4.2
  ],
  "index_rows": 517
 },
 {
  "area": "egig_west_flank",
  "collection": "ICESSN",
  "window": [
   "2009-01-01",
   "2019-12-31"
  ],
  "bbox": [
   -45.0,
   69.8,
   -43.0,
   70.2
  ],
  "query_km2": 3407.5,
  "cmr_hits": 89,
  "cmr_gb": 0.02,
  "cmr_hull": 233,
  "idx_cells": 208,
  "idx_box": 93,
  "false_pos": 0.107,
  "missed": 0,
  "need_mb": 13.226,
  "bytes_ratio": 1.7,
  "fp_area": 0.0,
  "fp_kind": "polygon",
  "chunks_per_gran": null,
  "gran_mb_quartiles": [
   0.1,
   0.1,
   0.5
  ],
  "index_rows": 355
 },
 {
  "area": "k_transect_upstream",
  "collection": "ATL06",
  "window": [
   "2018-10-01",
   "2027-01-01"
  ],
  "bbox": [
   -48.5,
   66.9,
   -46.5,
   67.3
  ],
  "query_km2": 3875.0,
  "cmr_hits": 557,
  "cmr_gb": 49.86,
  "cmr_hull": 649,
  "idx_cells": 579,
  "idx_box": 566,
  "false_pos": 0.111,
  "missed": 2,
  "need_mb": 625.513,
  "bytes_ratio": 79.7,
  "fp_area": 7.2,
  "fp_kind": "polygon",
  "chunks_per_gran": 5,
  "gran_mb_quartiles": [
   80.0,
   96.0,
   104.0
  ],
  "index_rows": 12748
 },
 {
  "area": "k_transect_upstream",
  "collection": "ATL03",
  "window": [
   "2018-10-01",
   "2027-01-01"
  ],
  "bbox": [
   -48.5,
   66.9,
   -46.5,
   67.3
  ],
  "query_km2": 3875.0,
  "cmr_hits": 567,
  "cmr_gb": 1102.51,
  "cmr_hull": 598,
  "idx_cells": 543,
  "idx_box": 517,
  "false_pos": 0.092,
  "missed": 0,
  "need_mb": 11737.723,
  "bytes_ratio": 93.9,
  "fp_area": 7.5,
  "fp_kind": "polygon",
  "chunks_per_gran": 115,
  "gran_mb_quartiles": [
   1024.0,
   1472.0,
   2680.0
  ],
  "index_rows": 36050
 },
 {
  "area": "k_transect_upstream",
  "collection": "GLAS",
  "window": [
   "2003-02-20",
   "2009-10-11"
  ],
  "bbox": [
   -48.5,
   66.9,
   -46.5,
   67.3
  ],
  "query_km2": 3875.0,
  "cmr_hits": 108,
  "cmr_gb": 0.4,
  "cmr_hull": 140,
  "idx_cells": 102,
  "idx_box": 102,
  "false_pos": 0.271,
  "missed": 0,
  "need_mb": 49.134,
  "bytes_ratio": 8.2,
  "fp_area": null,
  "fp_kind": "orbit",
  "chunks_per_gran": 1.0,
  "gran_mb_quartiles": [
   3.4,
   3.8,
   4.2
  ],
  "index_rows": 393
 },
 {
  "area": "k_transect_upstream",
  "collection": "ICESSN",
  "window": [
   "2009-01-01",
   "2019-12-31"
  ],
  "bbox": [
   -48.5,
   66.9,
   -46.5,
   67.3
  ],
  "query_km2": 3875.0,
  "cmr_hits": 321,
  "cmr_gb": 0.08,
  "cmr_hull": 556,
  "idx_cells": 513,
  "idx_box": 311,
  "false_pos": 0.077,
  "missed": 0,
  "need_mb": 56.745,
  "bytes_ratio": 1.4,
  "fp_area": 0.0,
  "fp_kind": "polygon",
  "chunks_per_gran": null,
  "gran_mb_quartiles": [
   0.1,
   0.1,
   0.5
  ],
  "index_rows": 934
 },
 {
  "area": "jakobshavn_margin",
  "collection": "ATL06",
  "window": [
   "2018-10-01",
   "2027-01-01"
  ],
  "bbox": [
   -51.0,
   69.0,
   -49.0,
   69.4
  ],
  "query_km2": 3537.5,
  "cmr_hits": 571,
  "cmr_gb": 47.08,
  "cmr_hull": 749,
  "idx_cells": 644,
  "idx_box": 626,
  "false_pos": 0.14,
  "missed": 0,
  "need_mb": 751.678,
  "bytes_ratio": 62.6,
  "fp_area": 7.2,
  "fp_kind": "polygon",
  "chunks_per_gran": 5.0,
  "gran_mb_quartiles": [
   64.0,
   88.0,
   96.0
  ],
  "index_rows": 13950
 },
 {
  "area": "jakobshavn_margin",
  "collection": "ATL03",
  "window": [
   "2018-10-01",
   "2027-01-01"
  ],
  "bbox": [
   -51.0,
   69.0,
   -49.0,
   69.4
  ],
  "query_km2": 3537.5,
  "cmr_hits": 576,
  "cmr_gb": 1410.38,
  "cmr_hull": 637,
  "idx_cells": 608,
  "idx_box": 574,
  "false_pos": 0.046,
  "missed": 0,
  "need_mb": 20080.562,
  "bytes_ratio": 70.2,
  "fp_area": 8.2,
  "fp_kind": "polygon",
  "chunks_per_gran": 109.0,
  "gran_mb_quartiles": [
   954.0,
   1400.0,
   3518.0
  ],
  "index_rows": 36308
 },
 {
  "area": "jakobshavn_margin",
  "collection": "GLAS",
  "window": [
   "2003-02-20",
   "2009-10-11"
  ],
  "bbox": [
   -51.0,
   69.0,
   -49.0,
   69.4
  ],
  "query_km2": 3537.5,
  "cmr_hits": 93,
  "cmr_gb": 0.35,
  "cmr_hull": 138,
  "idx_cells": 113,
  "idx_box": 113,
  "false_pos": 0.181,
  "missed": 0,
  "need_mb": 55.739,
  "bytes_ratio": 6.3,
  "fp_area": null,
  "fp_kind": "orbit",
  "chunks_per_gran": 1,
  "gran_mb_quartiles": [
   3.3,
   3.9,
   4.3
  ],
  "index_rows": 372
 },
 {
  "area": "jakobshavn_margin",
  "collection": "ICESSN",
  "window": [
   "2009-01-01",
   "2019-12-31"
  ],
  "bbox": [
   -51.0,
   69.0,
   -49.0,
   69.4
  ],
  "query_km2": 3537.5,
  "cmr_hits": 504,
  "cmr_gb": 0.14,
  "cmr_hull": 882,
  "idx_cells": 775,
  "idx_box": 492,
  "false_pos": 0.121,
  "missed": 0,
  "need_mb": 101.917,
  "bytes_ratio": 1.3,
  "fp_area": 0.0,
  "fp_kind": "polygon",
  "chunks_per_gran": null,
  "gran_mb_quartiles": [
   0.1,
   0.1,
   0.5
  ],
  "index_rows": 1452
 },
 {
  "area": "summit_traverse",
  "collection": "ATL06",
  "window": [
   "2018-10-01",
   "2027-01-01"
  ],
  "bbox": [
   -38.585667,
   72.578616,
   -38.474193,
   72.648222
  ],
  "query_km2": 28.9,
  "cmr_hits": 109,
  "cmr_gb": 9.02,
  "cmr_hull": 220,
  "idx_cells": 150,
  "idx_box": 140,
  "false_pos": 0.318,
  "missed": 0,
  "need_mb": 104.937,
  "bytes_ratio": 85.9,
  "fp_area": 852.3,
  "fp_kind": "polygon",
  "chunks_per_gran": 4.0,
  "gran_mb_quartiles": [
   72.0,
   88.0,
   96.0
  ],
  "index_rows": 676
 },
 {
  "area": "summit_traverse",
  "collection": "ATL03",
  "window": [
   "2018-10-01",
   "2027-01-01"
  ],
  "bbox": [
   -38.585667,
   72.578616,
   -38.474193,
   72.648222
  ],
  "query_km2": 28.9,
  "cmr_hits": 116,
  "cmr_gb": 234.43,
  "cmr_hull": 175,
  "idx_cells": 144,
  "idx_box": 96,
  "false_pos": 0.177,
  "missed": 0,
  "need_mb": 493.161,
  "bytes_ratio": 475.4,
  "fp_area": 1009.3,
  "fp_kind": "polygon",
  "chunks_per_gran": 84.5,
  "gran_mb_quartiles": [
   920.0,
   1404.0,
   2762.0
  ],
  "index_rows": 1375
 },
 {
  "area": "summit_traverse",
  "collection": "GLAS",
  "window": [
   "2003-02-20",
   "2009-10-11"
  ],
  "bbox": [
   -38.585667,
   72.578616,
   -38.474193,
   72.648222
  ],
  "query_km2": 28.9,
  "cmr_hits": 15,
  "cmr_gb": 0.06,
  "cmr_hull": 31,
  "idx_cells": 21,
  "idx_box": 21,
  "false_pos": 0.323,
  "missed": 0,
  "need_mb": 11.153,
  "bytes_ratio": 5.3,
  "fp_area": null,
  "fp_kind": "orbit",
  "chunks_per_gran": 1,
  "gran_mb_quartiles": [
   3.6,
   4.0,
   4.5
  ],
  "index_rows": 21
 },
 {
  "area": "summit_traverse",
  "collection": "ICESSN",
  "window": [
   "2009-01-01",
   "2019-12-31"
  ],
  "bbox": [
   -38.585667,
   72.578616,
   -38.474193,
   72.648222
  ],
  "query_km2": 28.9,
  "cmr_hits": 43,
  "cmr_gb": 0.01,
  "cmr_hull": 63,
  "idx_cells": 62,
  "idx_box": 43,
  "false_pos": 0.016,
  "missed": 0,
  "need_mb": 4.093,
  "bytes_ratio": 2.3,
  "fp_area": 0.3,
  "fp_kind": "polygon",
  "chunks_per_gran": null,
  "gran_mb_quartiles": [
   0.1,
   0.1,
   0.5
  ],
  "index_rows": 62
 }
]
```
