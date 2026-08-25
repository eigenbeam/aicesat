"""Rewrite lake files with 64k-row row groups so DuckDB can prune inside files (one-off after upgrading)."""
import json
from aicesat import lake
print(json.dumps(lake.relayout()))
