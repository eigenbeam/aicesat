"""A client that hangs up mid-response is normal traffic, not a server error.

The Data Lake view polls /api/index_status every 8 s and that call is slow enough to still be in flight when the view
is left, so the browser drops the socket routinely. socketserver's default handle_error printed a full traceback for
each one, which filled the journal with BrokenPipeError stacks and buried anything real.

The work has already succeeded at that point — only the delivery failed — so the right behaviour is to notice, close
the connection, and say nothing above debug.
"""
import http.client
import json
import socket
import threading

import pytest

from aicesat import server


@pytest.fixture
def srv():
    """A real Server on a real socket: the disconnect behaviour lives in socketserver, so a mocked handler proves
    nothing. Serves in a background thread and is torn down after the test."""
    try:
        s = server.Server(("127.0.0.1", 0), server.Handler)
    except (PermissionError, OSError) as e:   # sandboxed runners may forbid bind(); the behaviour is still real
        pytest.skip(f"cannot bind a local socket here: {e}")
    t = threading.Thread(target=s.serve_forever, daemon=True)
    t.start()
    yield s
    s.shutdown(); s.server_close(); t.join(5)


def test_write_helper_is_not_recursive():
    """_write wraps wfile.write; a careless edit once made it call itself, which is a hang, not a failed test."""
    import inspect
    src = inspect.getsource(server.Handler._write)
    assert "self.wfile.write(body)" in src
    assert "self._write(body)" not in src, "_write calls itself — infinite recursion on every response"


def test_a_vanished_client_is_not_logged_as_an_error(srv, monkeypatch):
    """The whole point: a mid-response disconnect must not reach the traceback-printing path."""
    seen, escaped = [], []
    real = server.Server.handle_error

    def spy(self, request, client_address):
        import sys
        exc = sys.exc_info()[1]
        seen.append(type(exc))
        if not isinstance(exc, server._CLIENT_GONE):
            escaped.append(exc)          # anything else would have printed a traceback
        return real(self, request, client_address)
    monkeypatch.setattr(server.Server, "handle_error", spy)

    host, port = srv.server_address[0], srv.server_address[1]
    # Send a request, then close the socket before reading the response — exactly what a browser navigating away does.
    c = socket.create_connection((host, port))
    c.sendall(b"GET /api/collections HTTP/1.1\r\nHost: x\r\n\r\n")
    c.close()

    # A second, complete request proves the server is still healthy after the hang-up.
    conn = http.client.HTTPConnection(host, port, timeout=10)
    conn.request("GET", "/api/collections")
    r = conn.getresponse()
    assert r.status == 200
    json.loads(r.read())
    conn.close()

    assert not escaped, f"a disconnect reached the error path as {escaped}"


def test_the_handler_survives_a_write_to_a_closed_socket():
    """_write must swallow the disconnect errors and mark the connection closed, not propagate."""
    class FakeWfile:
        def write(self, b):
            raise BrokenPipeError(32, "Broken pipe")

    h = server.Handler.__new__(server.Handler)     # no socket setup: only _write is under test
    h.wfile = FakeWfile()
    h.path = "/api/index_status"
    h.close_connection = False
    h._write(b"x" * 10)                            # must not raise
    assert h.close_connection is True
