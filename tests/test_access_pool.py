"""The HTTPS connection pool must cover the concurrency the fetch path actually creates.

fetch_bbox builds ONE RangeReader and hands it to an outer pool of up to FETCH_WORKER_CAP granules, each of which
fetches its spans with `threads` inside. The pool was a flat 32 against 16 x 8 = 128, and urllib3 does not queue
past pool_maxsize — it opens the connection, uses it once and discards it, so every overflow request pays a fresh
TLS handshake. A real scene build logged 24 "Connection pool is full, discarding connection" warnings.
"""
import pytest

from aicesat import access


def _pool_of(reader):
    return reader.session.get_adapter("https://example.invalid/")._pool_maxsize


def test_pool_covers_outer_times_inner_concurrency():
    r = access.RangeReader()
    assert _pool_of(r) >= access.FETCH_WORKER_CAP * r.threads


@pytest.mark.parametrize("threads", [1, 4, 8, 16, 32])
def test_pool_scales_with_the_inner_thread_count(threads):
    r = access.RangeReader(threads=threads)
    assert _pool_of(r) >= access.FETCH_WORKER_CAP * threads, f"threads={threads}"


def test_pool_never_drops_below_the_old_floor():
    """A single-threaded reader still shares the session with the outer pool; 32 was the previous value and is a
    sane floor regardless."""
    assert _pool_of(access.RangeReader(threads=1)) >= 32


def test_pool_follows_the_outer_worker_override(monkeypatch):
    """AICESAT_FETCH_WORKERS widens the OUTER pool, so the connection pool has to widen with it or the override
    silently reintroduces the churn it was set to avoid."""
    monkeypatch.setenv(access.FETCH_WORKER_ENV, "40")
    r = access.RangeReader(threads=4)
    assert _pool_of(r) >= 40 * 4


def test_pool_sizing_does_not_change_concurrency():
    """This is a keep-alive fix, not a throughput knob: the number of workers must be untouched."""
    assert access.FETCH_WORKER_CAP == 16
    assert access.RangeReader(threads=8).threads == 8
