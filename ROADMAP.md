# Working Order — `recost` Python SDK

Plan for working through the 13 GitHub issues from [`AUDIT.md`](./AUDIT.md). Issues are grouped into **waves**; each wave is a set that can be worked in parallel without conflict, and waves are sequential.

Mapping to filed issues: <https://github.com/recost-dev/middleware-python/issues>

---

## At-a-glance

| Wave | Issues | Parallel? | Reason for order |
|---|---|---|---|
| 1 — Foundation | #1, #5 | Yes (different files) | Both zero-risk for blocking other work. |
| 2 — Threading primitive | #4 | — | Adds module-level `RLock` that Wave 3 / 4 depend on. |
| 3 — Lifecycle | #6 then #7 | Sequential (both touch `_transport.py`) | #6 fixes dispose semantics first; #7 builds on clean dispose. |
| 4 — Fork-safety + body sizing | #3, #8 | Yes (different files) | #3 builds on Wave 3's clean dispose; #8 is `_interceptor.py`-only. |
| 5 — P2 cleanup + types + tests | #10, #11, #12, #13, #2, #9 | Mostly yes | Small surface; fold tests into each fix; mypy sweep cleans up residual. |

---

## Detailed sequencing

### Wave 1 — Foundation

Two independent tracks, fully parallel.

- **#1 — [Aggregator thread-safety](https://github.com/recost-dev/middleware-python/issues/1)** (P0)
  Files: `recost/_aggregator.py`, `tests/test_aggregator.py`
  Wrap `ingest` / `flush` / `would_overflow` in `threading.RLock`. Add a regression test that races N threads ingesting against a flush. No dependencies on other issues.

- **#5 — [Naming + docs reconciliation](https://github.com/recost-dev/middleware-python/issues/5)** (P1)
  Files: `recost/frameworks/flask.py`, `recost/__init__.py`, `README.md`, `CLAUDE.md`, `tests/test_provider_registry.py`
  Flask class rename (`ReCost` → `RecostExtension`, alias for one release), strip `EcoAPI*` from CLAUDE.md, fix the provider-count claims (21 → 34), document `flush_interval_ms`/`max_buckets`/`shutdown_flush_timeout_ms`, mark `flush_interval` deprecated. No business-logic risk.

**Why this order:** both are zero-risk for blocking other work. Naming should land before any test or doc changes elsewhere so subsequent commits don't reference the old names.

---

### Wave 2 — Threading primitive

- **#4 — [Module-level state races](https://github.com/recost-dev/middleware-python/issues/4)** (P1)
  Files: `recost/_init.py`, `recost/_interceptor.py`, `tests/test_init.py`
  Introduces a module-level `threading.RLock` around `init()` / `dispose()` / `install()` / `uninstall()`. **Must land before Wave 3** because #6 and #3 will both rely on the same lock for their atomicity guarantees.

---

### Wave 3 — Process lifecycle

Both edits live in `_transport.py`, so do them sequentially to avoid merge conflicts:

1. **#6 — [Process lifecycle: atexit + dispose-during-connect FD leak](https://github.com/recost-dev/middleware-python/issues/6)** (P1)
   Files: `recost/_init.py`, `recost/_transport.py`, `tests/test_init.py`
   Fixes the dispose path so the loop properly stops and the thread joins. Adds an `atexit` handler that runs a final flush. Provides the clean-dispose mechanic that #3 reuses.

2. **#7 — [Local-mode WebSocket hardening](https://github.com/recost-dev/middleware-python/issues/7)** (P1)
   Files: `recost/_transport.py`, `tests/test_transport.py`, plus a coordinated change in the extension repo
   Cap the queue (drop-oldest), give up after N failed reconnects (with a one-shot `on_error`), add lightweight `hello`/`ack` handshake. Doing this second means rebasing on top of cleaner dispose semantics.

---

### Wave 4 — Fork-safety + body sizing

Parallel — different files:

- **#3 — [No fork-safety](https://github.com/recost-dev/middleware-python/issues/3)** (P0)
  Files: `recost/_init.py`, `recost/_transport.py`, `README.md`, `tests/test_init.py`
  Register `os.register_at_fork(after_in_child=_reinit_after_fork)` that re-creates the timer thread and transport thread in the child. Builds on Wave 3's clean dispose because the child needs to fully discard the parent's transport state.

- **#8 — [Body-size measurement](https://github.com/recost-dev/middleware-python/issues/8)** (P1)
  Files: `recost/_interceptor.py`, `README.md`, `tests/test_interceptor.py`
  aiohttp `json=`/`FormData`, httpx streaming-body materialization, response Content-Length-only caveat. Independent of all lifecycle work.

---

### Wave 5 — P2 cleanup + types + tests

Most can be done in parallel; finish in any order.

- **#10 — [Flush loop hygiene](https://github.com/recost-dev/middleware-python/issues/10)** (P2) — `recost/_init.py`
- **#11 — [urllib3 wrapper maintenance](https://github.com/recost-dev/middleware-python/issues/11)** (P2) — `recost/_interceptor.py`
- **#12 — [exclude_patterns](https://github.com/recost-dev/middleware-python/issues/12)** (P2) — `recost/_init.py`, `README.md`
- **#13 — [Code hygiene](https://github.com/recost-dev/middleware-python/issues/13)** (P2) — `recost/_transport.py`, `recost/_interceptor.py`
- **#2 — [35 mypy strict errors](https://github.com/recost-dev/middleware-python/issues/2)** (P0) — touched files were largely fixed during earlier waves; this is the residual sweep. Add `mypy --strict` to CI as part of this PR so the strict-clean claim becomes verifiable.
- **#9 — [Test gaps](https://github.com/recost-dev/middleware-python/issues/9)** (P1) — fold tests into each PR above. Outstanding items by end of Wave 4: aiohttp interceptor branch coverage (Wave 4 dependency), privacy test, self-instrumentation test, 5xx retry test.

---

## Cross-cutting rules

- **One issue per PR.** Each PR closes one numbered issue. Avoid bundling unrelated fixes.
- **Tests with every fix.** Don't merge a bugfix without a test that fails before the fix and passes after.
- **mypy `--strict` must pass on touched files before merge.** This is how #2 closes — incrementally per file. By Wave 5 only residual errors should remain.
- **Update `CHANGELOG.md` with each PR** (or create one if absent).

---

## What to start with right now

Pick one of:

1. **#5** — low-risk warm-up that unblocks naming.
2. **#1** — go straight to a P0 with a focused fix.
3. **#4** — set up the threading primitive that 3 / 6 / 7 depend on (longer chain).

**Recommendation:** open two branches and do **#5 + #1 in parallel as Wave 1**, then start **#4**.
