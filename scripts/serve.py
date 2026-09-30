"""Run only the widget HTTP server (no MCP stdio) for local testing."""
import logging, sys, time
from aicesat.server import start_http, HTTP_PORT


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    start_http()
    print(f"serving on http://127.0.0.1:{HTTP_PORT}/  (Ctrl-C to stop)", file=sys.stderr)
    while True:
        time.sleep(3600)


# Guarded: under the spawn start method (macOS) every process-pool worker - the ATL03 planner's, the index builders' -
# imports this script as __mp_main__; unguarded, each started a second server and never took its work.
if __name__ == "__main__":
    main()
