# Changelog

All notable changes to the recost Python middleware are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.3] — 2026-05-21

Production-readiness pass. The SDK keeps the same public `init()` entry point
and call sites compile unchanged, but the substrate underneath is now hardened
against real-world failure modes: forked workers, short-lived processes, bad
API keys, rate limits, and a usable "no-cloud-account" path that does not need
the VS Code extension running.

### Added

- **Local file transport (new default).** When no `api_key` is configured,
  the SDK now writes each `WindowSummary` as an NDJSON line to
  `~/.recost/local-telemetry/{project_id}.jsonl` (or `$RECOST_LOCAL_DIR/{project_id}.jsonl`).
  POSIX permissions are tightened to `0o600` on create. Append-atomic for
  writes ≤ PIPE_BUF. Opt back into WebSocket with `local_transport="ws"`. (#38)
- **Wire-format protocol version.** Every transport frame now carries a
  top-level `protocolVersion: "1.0"`. Consumers must reject frames with an
  unknown MAJOR version. (#23)
- **Typed error hierarchy** routed through `on_error`:
  - `RecostError` — base class for all SDK errors.
  - `RecostAuthError(status, consecutive_failures)` — fired on each 401.
  - `RecostFatalAuthError` — fired once when the consecutive-401 streak
    reaches `max_consecutive_auth_failures` (default 5); transport then
    suspends until process restart.
  - `RecostRateLimitError(retry_after_ms, endpoint)` — fired on 429.
    The SDK has already parsed `Retry-After` and deferred the next flush.
- **`handle.flush_blocking(timeout_s=3.0) -> bool`.** Synchronously runs the
  final flush on the calling thread, bounded by `timeout_s`. Companion to
  `dispose()` for short-lived scripts, `os._exit()` paths, and test teardown.
  Brings Python to parity with Node's awaited `dispose()`. (#21)
- **`handle.reinit_after_fork()` + automatic fork hook.** The SDK registers
  `os.register_at_fork(after_in_child=...)` to rebuild the transport and
  flush timer in forked children (Gunicorn, multiprocessing). A PID
  backstop in the event path self-repairs the first time the hook does not
  fire (uWSGI lazy-fork, embedded runtimes). (#3)
- **`handle.last_flush_status`.** Exposes the outcome of the most recent
  flush (`status`, `window_size`, `timestamp`) for health checks and
  dashboards.
- **`atexit` handler at `init()`.** Short-lived processes (CLI, cron,
  Lambda, SIGTERM'd containers) now flush their final window automatically.
  Idempotent with explicit `dispose()`. Gate with `auto_shutdown_handlers=False`.
- **`exclude_hosts`** config field for exact host-name exclusion, distinct
  from `exclude_patterns` (substring match against `event.url` / `event.host`).
  Use this when you need to drop `api.example.com` without also dropping
  `myapi.example.com`. `exclude_patterns` now rejects asterisks at init
  with a clear `ValueError`. (#12)
- **`max_consecutive_auth_failures`** config field (default 5). Cloud
  transport suspends once the streak reaches this number. Reset on any
  non-401 outcome. Matches Node's `maxConsecutiveAuthFailures`. (#32)
- **429 Retry-After deferral.** Parses both seconds and HTTP-date forms;
  the next flush is deferred via a shared deferral cell rather than the
  window being dropped. (#18)
- **`api_key` format validation at `init()`.** Keys must begin with `rc-`.
  Invalid keys raise `ValueError` immediately instead of silently failing
  every flush. (#17)
- **Flush-loop exponential backoff.** After 5 consecutive flush failures,
  the timer defers further attempts with exponential backoff capped at
  300 s, and announces the backoff once per escalation through `on_error`. (#10)
- **CI matrix** running ruff + mypy + pytest across Python 3.9 – 3.12.

### Changed

- **WebSocket local transport is now opt-in** (`local_transport="ws"`).
  The VS Code extension does not currently host a WS server, so file
  transport is the default. WS users get bounded queue (1000 frames,
  drop-oldest, announces once per overflow episode) and capped reconnect
  attempts (10, jittered exponential backoff). (#7, #38)
- **Wire format**: `projectId` is no longer included on the wire — the API
  extracts it from the URL path (`/projects/{id}/telemetry`). The
  dataclass keeps it for in-process use. (#16, #22, #36)
- **Timestamps** on `WindowSummary` are now produced via `iso_now_ms_z()`
  for millisecond-precision UTC with a trailing `Z`, byte-identical to
  Node's `new Date().toISOString()`. (#22)
- **Flask extension renamed** to `RecostExtension`. `ReCost` remains as a
  deprecated alias that emits `DeprecationWarning` and will be removed in
  a future release.
- **Aggregator is now thread-safe** (`RLock` on ingest/flush). Concurrent
  ingest no longer loses events under load.
- **Init/dispose and install/uninstall** are now guarded by `RLock`. A
  second `init()` call cleanly disposes the first. (#4)
- **Reentrancy guard** now covers both async-task hops (via `contextvars`)
  and OS-thread hops (via `threading.local`). Prevents double-counting
  when libraries call each other internally across either boundary. (#15)
- **`User-Agent`** and `sdk_version` on the wire now report `0.1.3`.

### Fixed

- **Streaming bodies are never materialized.** httpx streaming requests and
  aiohttp async-iterable / FormData payloads now report `request_bytes=0`
  rather than buffering the iterator. Sized payloads (`bytes`, `str`,
  `json=`, FormData with known `_size`) are measured. (#8)
- **`_strip_query` uses `urlparse`** — drops the fragment AND any userinfo
  from the netloc, not just the query string. Substring-based predecessor
  was fragile for encoded `?` / `#` in path positions. (#19)
- **Auth-failure counter resets on every non-401 outcome.** A 200 / 403 /
  network error no longer leaves a stale counter creeping toward
  fatal-suspend. (#32)
- **Flask `init_app()`** now disposes the prior handle before reassigning,
  so repeated re-init in long-running apps does not leak timer threads. (#20)
- **`dispose()` no longer blocks on `connect()`.** The local WS dispose path
  schedules `loop.stop()` via `call_soon_threadsafe` instead of relying on
  a queue sentinel that the connect coroutine never sees.
- **mypy strict** across the whole package; bare `raise` preserves traceback
  context where wrappers previously discarded it. (#13)

### Internal

- **130+ → 224 tests** across 14 files, covering: streaming-body sizing,
  fragment/userinfo stripping, dual-guard reentrancy, 4xx/5xx retry
  behavior, 401 escalation + fatal-suspend at N, 429 deferral, WS
  graceful-no-op when `websockets` is missing, file transport
  permissions and overflow semantics, fork-hook + PID-backstop paths,
  thread-safety races on ingest and init.
- New `ROADMAP.md` and `AUDIT.md` track open issues and prior reviews.
- Test added that pins `BUILTIN_PROVIDERS` count at 34 so accidental
  registry additions cannot drift the documented number silently.

[0.1.3]: https://github.com/recost-dev/middleware-python/releases/tag/v0.1.3
