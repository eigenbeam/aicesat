"""Per-granule fetches hand each granule's points on as it lands, not after the slowest one.

fetch_bbox used to run `list(ex.map(...))` and only then call on_granule for every granule, so a build's stream got
each mission in one burst at the end of its fetch: ICESat-2 showed nothing for 10-20 s on a cold change map, then all
of it at once. The returned list stays in input order, so the final arrays do not depend on which GET won."""
import threading
import time

import pytest

from aicesat.access import map_as_completed


def test_each_result_is_handed_on_as_it_lands_and_the_return_keeps_input_order():
    release = threading.Event()
    seen = []

    def work(x):
        if x == "slow":
            assert release.wait(5), "the fast items were not handed on before the slow one finished"
        return x.upper()

    def on_each(r):
        seen.append(r)
        if r == "B":             # both fast ones are out: only now may the slow one finish
            release.set()

    out = map_as_completed(work, ["slow", "a", "b"], 3, on_each)
    assert out == ["SLOW", "A", "B"]                       # input order, whatever the completion order
    assert seen[-1] == "SLOW" and sorted(seen) == ["A", "B", "SLOW"]


def test_serial_when_one_worker():
    seen = []
    assert map_as_completed(lambda x: x * 2, [1, 2, 3], 1, seen.append) == [2, 4, 6]
    assert seen == [2, 4, 6]


def test_on_each_runs_on_the_callers_thread():
    """on_granule writes the scene's preview under the build's stream_lock; it must not run on pool threads."""
    me, where = threading.get_ident(), set()
    map_as_completed(lambda x: (time.sleep(0.01), x)[1], list(range(6)), 3, lambda r: where.add(threading.get_ident()))
    assert where == {me}


def test_a_failure_propagates():
    def work(x):
        if x == 2:
            raise ValueError("boom")
        return x
    with pytest.raises(ValueError, match="boom"):
        map_as_completed(work, [1, 2, 3], 2, None)
