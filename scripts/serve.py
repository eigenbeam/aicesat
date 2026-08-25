"""Run only the widget HTTP server (no MCP stdio) for local testing."""
import logging, sys, time
from aicesat.server import start_http, HTTP_PORT
logging.basicConfig(level=logging.INFO, stream=sys.stderr)
start_http()
print(f"serving on http://127.0.0.1:{HTTP_PORT}/  (Ctrl-C to stop)", file=sys.stderr)
while True:
    time.sleep(3600)
