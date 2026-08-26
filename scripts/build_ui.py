"""Build the single-file UI: src/aicesat/ui/* + vendored libs -> src/aicesat/widget/dist/aicesat.html (pure Python)."""
from aicesat.uibuild import build

if __name__ == "__main__":
    out = build()
    print(f"built {out} ({out.stat().st_size / 1e6:.2f} MB)")
