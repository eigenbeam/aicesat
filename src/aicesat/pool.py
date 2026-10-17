"""The one process pool the index builders run on.

Every builder once owned its own pool, and the five copies disagreed on the things that matter: `index.ensure_index`
spawned, retried, and gave each future a monotonic deadline; the other four forked (Linux's default, with the whole CMR
result in the parent and copy-on-write un-sharing it worker by worker), ran `ex.map` under one batch budget, and never
retried. This module is the policy written once, with no knowledge of granules or collections, so that `index.py` (a
query-path module) and `build.py` (the driver) can both use it without one importing the other.

  * SPAWN, not fork. A forked worker starts as a copy of the parent -- for an all-Greenland ATL03 build that is ~3 GB
    of CMR result -- and reference counting un-shares those pages until memory climbs towards (workers + 1) x 3 GB.
    Spawned workers receive only their task. (macOS already spawns; the deployed box is Linux and forks.)
  * MONOTONIC deadline per future, not wall clock. A laptop that sleeps mid-build advances time.time() by the whole
    nap, so every remaining future would blow a wall-clock deadline the moment the lid opens and a healthy build would
    report itself failed; time.monotonic() excludes suspend on both macOS and Linux.
  * EVERY worker failure is retryable, not only a timeout. A transient 503 killed a 120-granule build at granule 101
    because only FutTimeout was caught, and it surfaced as "can't pickle multidict.CIMultiDictProxy": aiohttp's error
    carries response headers the pool cannot pickle back, so the real cause is destroyed in transit. One flaky granule
    must never discard the other 119.
  * Futures are consumed in SUBMISSION order with a per-future timeout, not with as_completed. The order is what a
    progress callback sees, and a result that arrives early simply waits its turn.

The constructor is called with exactly `max_workers` and `mp_context`, looked up on `concurrent.futures` at call time,
so tests can stand in a serial pool (tests/test_index_pool_memory.py, tests/test_index_atl06_extract.py).
"""
from __future__ import annotations

import logging
import multiprocessing
import time
from typing import Callable

log = logging.getLogger(__name__)

ATTEMPTS = 2


def run_pool(fn: Callable, items: list, *, args: tuple = (), workers: int, timeout_s: float, name_of: Callable = repr,
             attempts: int = ATTEMPTS, on_each: Callable | None = None) -> tuple[list, list]:
    """Run `fn(item, *args)` for every item in a spawn pool of `workers` processes.

    `timeout_s` is the allowance per item; the deadline for a batch is `timeout_s * (items / workers + 1)` from the
    moment it is submitted. Items that fail or time out are retried in a fresh pool, up to `attempts` times in all.
    `on_each(item, result)` fires in submission order as each result arrives.

    Returns `(done, failed)`: `done` is `[(item, result), ...]` in submission order across attempts; `failed` is
    `[(item, error_text), ...]` for the items that failed their last attempt.
    """
    import concurrent.futures as cf

    todo = list(items)
    done: list = []
    last_error: dict[int, str] = {}
    for attempt in range(1, attempts + 1):
        if not todo:
            break
        failed = []
        n = max(1, min(workers, len(todo)))
        ex = cf.ProcessPoolExecutor(max_workers=n, mp_context=multiprocessing.get_context("spawn"))
        futs = [(ex.submit(fn, it, *args), it) for it in todo]
        deadline = time.monotonic() + timeout_s * (len(todo) / n + 1)
        for f, it in futs:
            try:
                r = f.result(timeout=max(1.0, deadline - time.monotonic()))
            except cf.TimeoutError:
                log.warning("%s timed out (attempt %d)", name_of(it), attempt)
                last_error[id(it)] = f"timed out after {timeout_s:.0f} s"
                failed.append(it)
                continue
            except Exception as e:  # noqa: BLE001 - every worker failure is retryable, see the module docstring
                log.warning("%s failed (attempt %d): %s: %s", name_of(it), attempt, type(e).__name__, e)
                last_error[id(it)] = f"{type(e).__name__}: {e}"
                failed.append(it)
                continue
            done.append((it, r))
            if on_each is not None:
                on_each(it, r)
        ex.shutdown(wait=False, cancel_futures=True)
        todo = failed
    return done, [(it, last_error.get(id(it), "failed")) for it in todo]
