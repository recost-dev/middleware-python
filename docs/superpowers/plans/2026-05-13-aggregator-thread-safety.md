# Aggregator Thread-Safety Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `recost._aggregator.Aggregator` safe to use concurrently from the interceptor's user threads (calling `ingest`) and the background timer thread (calling `flush`), eliminating the `RuntimeError: dictionary changed size during iteration` race documented in [issue #1](https://github.com/recost-dev/middleware-python/issues/1).

**Architecture:** Add a single `threading.RLock` on `Aggregator`. Use a swap-and-process pattern in `flush`: under the lock, swap the buckets dict for a fresh empty one and capture window state; release the lock; then run the (potentially slow) percentile + summary construction work outside the critical section. Lock `ingest`, `would_overflow`, and the `size` / `bucket_count` properties for consistent snapshots. Add a stress test that races N ingester threads against a flusher thread and a correctness test that proves no events are lost.

**Tech Stack:** Python ≥ 3.9 stdlib (`threading.RLock`, `concurrent.futures`), pytest, no new dependencies.

---

## File Structure

| File | Role |
|---|---|
| `recost/_aggregator.py` | Add `self._lock` to `__init__`; wrap mutating methods; refactor `flush` to swap-and-process. |
| `tests/test_aggregator.py` | Add `TestThreadSafety` class with two tests (no-exception stress test, no-lost-events correctness test). |

Nothing else is touched. The change is fully isolated — no signature changes, no public API churn.

---

## Task 1: Add the failing thread-safety stress test

**Files:**
- Test: `tests/test_aggregator.py` (append a new `TestThreadSafety` class at the bottom)

- [ ] **Step 1: Write the failing stress test**

Append to `tests/test_aggregator.py` (after `class TestBucketOverflow` and before EOF):

```python
# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------

import threading


class TestThreadSafety:
    """Regression tests for issue #1 — Aggregator must be safe under concurrent
    ingest (user threads) + flush (timer thread)."""

    def test_concurrent_ingest_and_flush_does_not_raise(self):
        """Stress: 4 ingester threads run 5000 ingests each (20k total) while
        one flusher thread runs 100 flushes. Pre-fix this reliably raises
        ``RuntimeError: dictionary changed size during iteration``.
        Post-fix it must complete without any exception."""
        agg = Aggregator()
        exceptions: list[BaseException] = []
        iterations_per_ingester = 5000
        num_ingesters = 4
        num_flushes = 100

        def ingester() -> None:
            try:
                for i in range(iterations_per_ingester):
                    p = f"p{i % 20}"
                    agg.ingest(make_event(provider=p, endpoint_category=p))
            except BaseException as exc:  # noqa: BLE001 — we want everything
                exceptions.append(exc)

        def flusher() -> None:
            try:
                for _ in range(num_flushes):
                    agg.flush()
            except BaseException as exc:  # noqa: BLE001
                exceptions.append(exc)

        threads = [threading.Thread(target=ingester) for _ in range(num_ingesters)]
        threads.append(threading.Thread(target=flusher))
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15.0)

        assert not exceptions, f"unexpected exceptions: {exceptions!r}"
        # Sanity: at least some threads ran to completion.
        for t in threads:
            assert not t.is_alive(), "a worker thread is still running"
```

- [ ] **Step 2: Run the test and confirm it fails on the current code**

Run: `python -m pytest tests/test_aggregator.py::TestThreadSafety::test_concurrent_ingest_and_flush_does_not_raise -v`

Expected: **FAIL** with one of:
- `AssertionError: unexpected exceptions: [RuntimeError('dictionary changed size during iteration'), ...]`
- Or, on some Python builds, the lost-update side of the race may surface as a different exception.

If the test passes pre-fix (rare but possible on a fast / cold machine), bump `iterations_per_ingester` to `20000` and re-run. It must reliably fail before continuing.

- [ ] **Step 3: Do NOT commit yet**

The test goes in the same commit as the fix (Task 2) so the repo never has a known-failing test on `main`.

---

## Task 2: Add the lock and refactor `flush` to swap-and-process

**Files:**
- Modify: `recost/_aggregator.py`

- [ ] **Step 1: Add `threading` import and `self._lock` to `__init__`**

Edit `recost/_aggregator.py`. Add `import threading` at the top with the other imports, and add the lock in `__init__`.

Find:

```python
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from ._types import MetricEntry, RawEvent, WindowSummary
```

Replace with:

```python
from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from ._types import MetricEntry, RawEvent, WindowSummary
```

Find:

```python
    def __init__(
        self,
        project_id: str = "",
        environment: str = "development",
        sdk_version: str = "0.0.0",
        max_buckets: int = MAX_BUCKETS,
    ) -> None:
        self._project_id = project_id
        self._environment = environment
        self._sdk_version = sdk_version
        self._max_buckets = max_buckets
        self._buckets: Dict[str, _Bucket] = {}
        self._window_start: Optional[str] = None
        self._size = 0
```

Replace with:

```python
    def __init__(
        self,
        project_id: str = "",
        environment: str = "development",
        sdk_version: str = "0.0.0",
        max_buckets: int = MAX_BUCKETS,
    ) -> None:
        self._project_id = project_id
        self._environment = environment
        self._sdk_version = sdk_version
        self._max_buckets = max_buckets
        self._buckets: Dict[str, _Bucket] = {}
        self._window_start: Optional[str] = None
        self._size = 0
        # RLock so a single thread can re-enter (e.g. if a future event
        # callback ever calls back into the aggregator). Guards every method
        # below that reads or writes _buckets / _window_start / _size.
        self._lock = threading.RLock()
```

- [ ] **Step 2: Wrap `would_overflow` in the lock**

Find:

```python
    def would_overflow(self, event: RawEvent) -> bool:
        """True if ingesting ``event`` would allocate a new bucket while the
        window is already at capacity. Callers should flush before ingesting."""
        if len(self._buckets) < self._max_buckets:
            return False
        return self._key_for(event) not in self._buckets
```

Replace with:

```python
    def would_overflow(self, event: RawEvent) -> bool:
        """True if ingesting ``event`` would allocate a new bucket while the
        window is already at capacity. Callers should flush before ingesting."""
        with self._lock:
            if len(self._buckets) < self._max_buckets:
                return False
            return self._key_for(event) not in self._buckets
```

- [ ] **Step 3: Wrap `ingest` in the lock**

Find:

```python
    def ingest(self, event: RawEvent, cost_cents: float = 0.0) -> None:
        """Add one RawEvent to the current window."""
        if self._window_start is None:
            self._window_start = event.timestamp

        provider = event.provider if event.provider is not None else "unknown"
        endpoint = event.endpoint_category if event.endpoint_category is not None else event.path
        key = self._key_for(event)

        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(provider=provider, endpoint=endpoint, method=event.method)
            self._buckets[key] = bucket

        bucket.request_count += 1
        if event.error:
            bucket.error_count += 1
        bucket.latencies.append(event.latency_ms)
        bucket.total_request_bytes += event.request_bytes
        bucket.total_response_bytes += event.response_bytes
        bucket.estimated_cost_cents += cost_cents

        self._size += 1
```

Replace with:

```python
    def ingest(self, event: RawEvent, cost_cents: float = 0.0) -> None:
        """Add one RawEvent to the current window."""
        with self._lock:
            if self._window_start is None:
                self._window_start = event.timestamp

            provider = event.provider if event.provider is not None else "unknown"
            endpoint = event.endpoint_category if event.endpoint_category is not None else event.path
            key = self._key_for(event)

            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(provider=provider, endpoint=endpoint, method=event.method)
                self._buckets[key] = bucket

            bucket.request_count += 1
            if event.error:
                bucket.error_count += 1
            bucket.latencies.append(event.latency_ms)
            bucket.total_request_bytes += event.request_bytes
            bucket.total_response_bytes += event.response_bytes
            bucket.estimated_cost_cents += cost_cents

            self._size += 1
```

- [ ] **Step 4: Refactor `flush` to swap-and-process**

The current `flush` holds state while building the summary — that means sorting latencies for thousands of buckets runs inside the critical section. Swap the buckets dict atomically, then process the snapshot without the lock.

Find:

```python
    def flush(self) -> Optional[WindowSummary]:
        """Compress the current window into a WindowSummary and reset state."""
        if not self._buckets:
            return None

        window_start = self._window_start or datetime.now(timezone.utc).isoformat()
        window_end = datetime.now(timezone.utc).isoformat()

        metrics: List[MetricEntry] = []

        for bucket in self._buckets.values():
            sorted_latencies = sorted(bucket.latencies)
            total_latency_ms = sum(sorted_latencies)

            metrics.append(MetricEntry(
                provider=bucket.provider,
                endpoint=bucket.endpoint,
                method=bucket.method,
                request_count=bucket.request_count,
                error_count=bucket.error_count,
                total_latency_ms=total_latency_ms,
                p50_latency_ms=_compute_percentile(sorted_latencies, 0.5),
                p95_latency_ms=_compute_percentile(sorted_latencies, 0.95),
                total_request_bytes=bucket.total_request_bytes,
                total_response_bytes=bucket.total_response_bytes,
                estimated_cost_cents=bucket.estimated_cost_cents,
            ))

        # Reset
        self._buckets = {}
        self._window_start = None
        self._size = 0

        return WindowSummary(
            project_id=self._project_id,
            environment=self._environment,
            sdk_language="python",
            sdk_version=self._sdk_version,
            window_start=window_start,
            window_end=window_end,
            metrics=metrics,
        )
```

Replace with:

```python
    def flush(self) -> Optional[WindowSummary]:
        """Compress the current window into a WindowSummary and reset state.

        Swap-and-process: under the lock, take ownership of the current
        buckets dict and reset state; then sort/percentile-compute outside
        the lock so ingest is not blocked for the duration of the flush.
        """
        with self._lock:
            if not self._buckets:
                return None
            buckets_to_flush = self._buckets
            window_start_captured = self._window_start
            # Reset state before releasing the lock so concurrent ingests
            # land in the *next* window, not this one.
            self._buckets = {}
            self._window_start = None
            self._size = 0

        # Outside the lock: format timestamps and build the summary.
        window_end = datetime.now(timezone.utc).isoformat()
        window_start = window_start_captured or window_end

        metrics: List[MetricEntry] = []
        for bucket in buckets_to_flush.values():
            sorted_latencies = sorted(bucket.latencies)
            total_latency_ms = sum(sorted_latencies)

            metrics.append(MetricEntry(
                provider=bucket.provider,
                endpoint=bucket.endpoint,
                method=bucket.method,
                request_count=bucket.request_count,
                error_count=bucket.error_count,
                total_latency_ms=total_latency_ms,
                p50_latency_ms=_compute_percentile(sorted_latencies, 0.5),
                p95_latency_ms=_compute_percentile(sorted_latencies, 0.95),
                total_request_bytes=bucket.total_request_bytes,
                total_response_bytes=bucket.total_response_bytes,
                estimated_cost_cents=bucket.estimated_cost_cents,
            ))

        return WindowSummary(
            project_id=self._project_id,
            environment=self._environment,
            sdk_language="python",
            sdk_version=self._sdk_version,
            window_start=window_start,
            window_end=window_end,
            metrics=metrics,
        )
```

- [ ] **Step 5: Wrap `size` and `bucket_count` properties in the lock**

Find:

```python
    @property
    def size(self) -> int:
        """Total events ingested since the last flush."""
        return self._size

    @property
    def bucket_count(self) -> int:
        """Number of unique provider + endpoint + method groups."""
        return len(self._buckets)
```

Replace with:

```python
    @property
    def size(self) -> int:
        """Total events ingested since the last flush."""
        with self._lock:
            return self._size

    @property
    def bucket_count(self) -> int:
        """Number of unique provider + endpoint + method groups."""
        with self._lock:
            return len(self._buckets)
```

(`max_buckets` is configured at construction and is immutable for the lifetime of the instance — no lock needed.)

- [ ] **Step 6: Run the stress test from Task 1 and confirm it passes**

Run: `python -m pytest tests/test_aggregator.py::TestThreadSafety::test_concurrent_ingest_and_flush_does_not_raise -v`

Expected: **PASS** within ~5 seconds.

- [ ] **Step 7: Run the full aggregator test suite to confirm no regressions**

Run: `python -m pytest tests/test_aggregator.py -v`

Expected: every existing test still passes (the original suite has ~40 cases — count varies; all must remain green).

- [ ] **Step 8: Commit the test + fix together**

```bash
git add recost/_aggregator.py tests/test_aggregator.py
git commit -m "fix(aggregator): make Aggregator thread-safe with RLock

Guards _buckets / _window_start / _size with a threading.RLock so the
background timer thread's flush() cannot race the interceptor's user
threads ingesting events. flush() now uses a swap-and-process pattern
so percentile computation runs outside the lock.

Adds a stress-test regression that reliably reproduced the race on the
old code (RuntimeError: dictionary changed size during iteration).

Closes #1"
```

---

## Task 3: Add a correctness test that proves no events are lost

The stress test above only proves \"no exception is raised\". This task adds a second test that proves the lock also fixes the lost-update race on counter increments (`bucket.request_count += 1` is *not* atomic across threads even with a dict-level lock — but `with self._lock` around the whole `ingest` body covers it).

**Files:**
- Test: `tests/test_aggregator.py` (extend `TestThreadSafety`)

- [ ] **Step 1: Append the correctness test**

Inside the existing `class TestThreadSafety:` (after `test_concurrent_ingest_and_flush_does_not_raise`):

```python
    def test_concurrent_correctness_no_lost_events(self):
        """4 ingester threads each push 2000 events while a flusher pulls
        windows concurrently. The total request_count summed across every
        flushed summary, plus the request_count of the final drain, must
        equal the total number of events ingested. Pre-fix this fails
        because ``bucket.request_count += 1`` races across threads."""
        agg = Aggregator()
        iterations_per_ingester = 2000
        num_ingesters = 4
        total_expected = iterations_per_ingester * num_ingesters
        flushed_total = 0
        flushed_lock = threading.Lock()  # this lock is in the *test*, not the SUT
        exceptions: list[BaseException] = []
        ingest_done = threading.Event()

        def ingester() -> None:
            try:
                for i in range(iterations_per_ingester):
                    # Use a small key space so several events land in the same
                    # bucket and the counter increment is contested.
                    p = f"p{i % 3}"
                    agg.ingest(make_event(provider=p, endpoint_category=p))
            except BaseException as exc:  # noqa: BLE001
                exceptions.append(exc)

        def flusher() -> None:
            nonlocal flushed_total
            try:
                while not ingest_done.is_set():
                    summary = agg.flush()
                    if summary is not None:
                        n = sum(m.request_count for m in summary.metrics)
                        with flushed_lock:
                            flushed_total += n
            except BaseException as exc:  # noqa: BLE001
                exceptions.append(exc)

        ingesters = [threading.Thread(target=ingester) for _ in range(num_ingesters)]
        flusher_thread = threading.Thread(target=flusher)
        flusher_thread.start()
        for t in ingesters:
            t.start()
        for t in ingesters:
            t.join(timeout=15.0)
        ingest_done.set()
        flusher_thread.join(timeout=5.0)

        # Final drain — anything ingested after the last flusher iteration.
        final = agg.flush()
        if final is not None:
            flushed_total += sum(m.request_count for m in final.metrics)

        assert not exceptions, f"unexpected exceptions: {exceptions!r}"
        assert flushed_total == total_expected, (
            f"lost events: flushed {flushed_total}, expected {total_expected}"
        )
```

- [ ] **Step 2: Run the correctness test and confirm it passes**

Run: `python -m pytest tests/test_aggregator.py::TestThreadSafety::test_concurrent_correctness_no_lost_events -v`

Expected: **PASS**.

If it fails (`lost events: flushed N, expected M`), it means the lock isn't wrapping the full `ingest` body. Re-check Task 2 Step 3.

- [ ] **Step 3: Commit**

```bash
git add tests/test_aggregator.py
git commit -m "test(aggregator): assert concurrent ingest does not lose events

Adds a second thread-safety test that proves no counter increments are
lost when N threads race on a shared bucket. Complements the no-raise
stress test by exercising the counter-increment side of the race."
```

---

## Task 4: Final verification — lint, types, full suite

- [ ] **Step 1: Run the full test suite**

Run: `python -m pytest`

Expected: every test passes (including the 130-ish existing tests and the 2 new ones).

- [ ] **Step 2: Run ruff**

Run: `python -m ruff check recost/ tests/`

Expected: no new errors. (Pre-existing `F401 MAX_BUCKETS` in `_transport.py` is a separate issue, [#13](https://github.com/recost-dev/middleware-python/issues/13), and is out of scope.)

- [ ] **Step 3: Run mypy on the changed file**

Run: `python -m mypy recost/_aggregator.py`

Expected: no new errors introduced by this change. (The repo overall has 35 known mypy errors tracked under [#2](https://github.com/recost-dev/middleware-python/issues/2); this PR must not increase that count for `_aggregator.py`.)

- [ ] **Step 4: Open the PR**

```bash
git push -u origin <branch-name>
gh pr create --title "fix(aggregator): make Aggregator thread-safe (closes #1)" --body "$(cat <<'EOF'
## Summary

Closes #1.

Guards `Aggregator` state with a `threading.RLock` so the background timer
thread's `flush()` cannot race user threads calling `ingest()` via the
interceptor. `flush()` uses a swap-and-process pattern so percentile
computation runs outside the critical section.

## Tests

- New `TestThreadSafety::test_concurrent_ingest_and_flush_does_not_raise` —
  4 ingester threads × 5000 events vs. 100 flushes; reliably reproduced
  the `RuntimeError: dictionary changed size during iteration` on the
  old code.
- New `TestThreadSafety::test_concurrent_correctness_no_lost_events` —
  proves counter increments are not lost across threads.

## Notes

- Public API unchanged.
- No new dependencies.
- Out of scope: thread-safety of `Aggregator.size` callers in `_init.py` —
  the property is now snapshot-consistent, but the calling pattern itself
  is part of issue #4.
EOF
)"
```

---

## Self-review

- **Spec coverage:** The filed issue body requires (a) RLock around `ingest`/`flush`/`would_overflow` — done in Task 2 steps 2–4; (b) a regression test pumping N threads — done in Task 1 and Task 3.
- **Placeholder scan:** No TBDs. Every code step shows the exact diff or test body.
- **Type consistency:** `Dict[str, _Bucket]`, `List[MetricEntry]`, `Optional[str]`, `Optional[WindowSummary]` all match the existing module's type style; `list[BaseException]` in the test files is Python 3.9-compatible because the test module already uses `from __future__ import annotations` implicitly via no annotations — re-check at execution time and switch to `List[BaseException]` if mypy complains under `--strict`.
- **Dependencies on other issues:** None. This is the first wave-1 item; the worktree is created from `main`.
