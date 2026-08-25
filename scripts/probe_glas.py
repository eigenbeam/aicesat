"""Print the Data_40HZ dataset layout of the first GLAH06 granule over a region (structure verification)."""
import sys, earthaccess, h5py
from aicesat import auth, coverage, regions
auth.login()
bbox = regions.resolve_bbox(sys.argv[1] if len(sys.argv) > 1 else regions.DEFAULT_REGION)
g = coverage.search("GLAH06", "034", bbox, ("2005-10-21", "2005-11-24"))[:1]
print(g[0]["meta"]["native-id"], "size MB", g[0].size())
f = h5py.File(earthaccess.open(g, show_progress=False)[0], "r")
def walk(name, obj):
    if isinstance(obj, h5py.Dataset) and name.startswith("Data_40HZ"):
        print(name, obj.shape, obj.dtype, {k: obj.attrs[k] for k in ("_FillValue", "units") if k in obj.attrs})
f.visititems(walk)
