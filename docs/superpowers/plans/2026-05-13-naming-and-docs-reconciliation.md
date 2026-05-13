# Naming and Docs Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Unify naming and documentation across the Python SDK so it has a single brand spelling (`Recost`), a single Flask-extension class name (`RecostExtension`, with a one-release `ReCost` deprecation alias), and accurate docs (real provider count, real config fields, no references to nonexistent `EcoAPI*` types). Closes [issue #5](https://github.com/recost-dev/middleware-python/issues/5).

**Architecture:**
- Rename the Flask extension class `ReCost` → `RecostExtension` in `recost/frameworks/flask.py`. Keep `ReCost = RecostExtension` as a deprecated alias that emits a `DeprecationWarning` on instantiation so external consumers don't break in this release. Document removal in a future major.
- Add `RecostExtension` to the public surface in `recost/__init__.py`.
- Add a regression test in `tests/test_provider_registry.py` asserting `len(BUILTIN_PROVIDERS) == 34` so future drift trips CI.
- Walk `CLAUDE.md` and remove every `EcoAPI*` reference (none exist in code).
- Walk `README.md`: rename `ReCost` → `RecostExtension` in the Flask example, replace the `flush_interval` row in the config table with `flush_interval_ms` (canonical) + a deprecated-`flush_interval` row, add rows for `max_buckets` and `shutdown_flush_timeout_ms`.

**Tech Stack:** Python ≥ 3.9 stdlib (`warnings`), pytest, no new dependencies.

---

## File Structure

| File | Role |
|---|---|
| `recost/frameworks/flask.py` | Rename `ReCost` → `RecostExtension`; add deprecation alias. |
| `recost/__init__.py` | Re-export `RecostExtension` (and the deprecation alias). |
| `tests/test_flask.py` | Update existing imports to new name; add deprecation-warning test. |
| `tests/test_provider_registry.py` | Add `len(BUILTIN_PROVIDERS) == 34` assertion. |
| `CLAUDE.md` | Strip `EcoAPI*`, fix provider-count claims, fix file-purpose comments. |
| `README.md` | Update Flask example, replace config table rows. |

No code in `_init.py`, `_interceptor.py`, `_transport.py`, `_aggregator.py`, `_types.py`, or `_provider_registry.py` changes. This is a docs-and-rename PR.

---

## Task 1: Rename the Flask class with a deprecation alias (TDD)

**Files:**
- Modify: `recost/frameworks/flask.py`
- Test: `tests/test_flask.py`

- [ ] **Step 1: Write the failing test for the new name + deprecation alias**

Replace the entire contents of `tests/test_flask.py` with:

```python
"""
Tests for recost/frameworks/flask.py
"""

import warnings

import pytest

from recost._interceptor import is_installed, uninstall
from recost._types import RecostConfig


class TestRecostExtension:
    def test_extension_initializes_interceptor(self):
        try:
            from flask import Flask
            from recost.frameworks.flask import RecostExtension

            app = Flask(__name__)
            RecostExtension(app, config=RecostConfig(enabled=True))
            assert is_installed()
        finally:
            uninstall()

    def test_extension_init_app_pattern(self):
        try:
            from flask import Flask
            from recost.frameworks.flask import RecostExtension

            ext = RecostExtension()
            app = Flask(__name__)
            ext.init_app(app, config=RecostConfig(enabled=True))
            assert is_installed()
        finally:
            uninstall()

    def test_extension_accepts_kwargs(self):
        try:
            from flask import Flask
            from recost.frameworks.flask import RecostExtension

            app = Flask(__name__)
            RecostExtension(app, enabled=True, debug=False)
            assert is_installed()
        finally:
            uninstall()


class TestReCostDeprecationAlias:
    """The old `ReCost` name must keep working for one release but emit a
    DeprecationWarning so users migrate to `RecostExtension`."""

    def test_old_name_still_constructs(self):
        try:
            from flask import Flask
            from recost.frameworks.flask import ReCost

            app = Flask(__name__)
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                ReCost(app, config=RecostConfig(enabled=True))
            assert is_installed()
            # At least one DeprecationWarning was emitted naming the new class.
            deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
            assert deprecations, "expected a DeprecationWarning on ReCost(...)"
            assert "RecostExtension" in str(deprecations[0].message)
        finally:
            uninstall()

    def test_old_name_is_subclass_or_alias_of_new(self):
        """Importing the old name must yield the same class as the new name
        so isinstance checks and existing type annotations keep working."""
        from recost.frameworks.flask import ReCost, RecostExtension

        # Either `ReCost is RecostExtension` (pure alias) or
        # `issubclass(ReCost, RecostExtension)` (thin subclass with __init__
        # that emits the warning). Both shapes are acceptable.
        assert ReCost is RecostExtension or issubclass(ReCost, RecostExtension)
```

- [ ] **Step 2: Run the new tests and confirm they fail**

Run: `python -m pytest tests/test_flask.py -v`

Expected: every `TestRecostExtension::*` test FAILS with `ImportError: cannot import name 'RecostExtension'`. The deprecation-alias tests fail at import for the same reason.

- [ ] **Step 3: Rename the class and add the deprecation alias**

Replace `recost/frameworks/flask.py` contents with:

```python
"""
Flask extension adapter for recost.

Usage:
    from flask import Flask
    from recost.frameworks.flask import RecostExtension

    app = Flask(__name__)
    ext = RecostExtension(app, api_key="...", project_id="...")
"""

from __future__ import annotations

import warnings
from typing import Any, Optional

from .._init import init
from .._types import RecostConfig

try:
    from flask import Flask

    class RecostExtension:
        """Flask extension that initializes Recost telemetry."""

        def __init__(
            self,
            app: Optional[Flask] = None,
            config: Optional[RecostConfig] = None,
            **kwargs: Any,
        ) -> None:
            self._handle = None
            if app is not None:
                self.init_app(app, config, **kwargs)

        def init_app(
            self,
            app: Flask,
            config: Optional[RecostConfig] = None,
            **kwargs: Any,
        ) -> None:
            if config is None:
                config = RecostConfig(**kwargs)
            self._handle = init(config)

    class ReCost(RecostExtension):
        """Deprecated alias for :class:`RecostExtension`.

        Will be removed in a future release. Switch to
        ``from recost.frameworks.flask import RecostExtension``.
        """

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            warnings.warn(
                "recost.frameworks.flask.ReCost is deprecated and will be "
                "removed in a future release; use RecostExtension instead.",
                DeprecationWarning,
                stacklevel=2,
            )
            super().__init__(*args, **kwargs)

except ImportError:
    class RecostExtension:  # type: ignore[no-redef]
        """Stub — install 'flask' to use: pip install recost[flask]"""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise ImportError(
                "flask is required for the Recost extension. "
                "Install it with: pip install recost[flask]"
            )

    class ReCost(RecostExtension):  # type: ignore[no-redef]
        """Stub deprecated alias — install 'flask' to use."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            warnings.warn(
                "recost.frameworks.flask.ReCost is deprecated; use "
                "RecostExtension instead.",
                DeprecationWarning,
                stacklevel=2,
            )
            super().__init__(*args, **kwargs)
```

- [ ] **Step 4: Run the Flask tests and confirm they pass**

Run: `python -m pytest tests/test_flask.py -v`

Expected: all 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add recost/frameworks/flask.py tests/test_flask.py
git commit -m "feat(flask): rename ReCost to RecostExtension; deprecate old name

The Flask extension class is renamed to RecostExtension to match the
naming convention of RecostMiddleware (FastAPI) and the rest of the
public surface. The old ReCost class remains importable as a thin
subclass that emits a DeprecationWarning on construction.

Refs #5"
```

---

## Task 2: Expose `RecostExtension` on the public `recost` surface

**Files:**
- Modify: `recost/__init__.py`

- [ ] **Step 1: Add `RecostExtension` to the top-level re-exports**

Replace `recost/__init__.py` contents with:

```python
"""
recost — Python SDK for Recost.

Tracks outbound HTTP API calls and reports cost, latency, and usage patterns
to the Recost dashboard or your local VS Code extension.
"""

from ._types import (
    FlushStatus,
    RecostConfig,
    MetricEntry,
    ProviderDef,
    RawEvent,
    TransportMode,
    WindowSummary,
)
from ._init import RecostHandle, init
from ._provider_registry import BUILTIN_PROVIDERS, MatchResult, ProviderRegistry
from ._interceptor import install, uninstall, is_installed
from ._aggregator import Aggregator, MAX_BUCKETS

__all__ = [
    "init",
    "RecostHandle",
    "RawEvent",
    "MetricEntry",
    "WindowSummary",
    "ProviderDef",
    "RecostConfig",
    "TransportMode",
    "FlushStatus",
    "ProviderRegistry",
    "BUILTIN_PROVIDERS",
    "MatchResult",
    "install",
    "uninstall",
    "is_installed",
    "Aggregator",
    "MAX_BUCKETS",
]
```

Note: framework adapters (`flask.RecostExtension`, `fastapi.RecostMiddleware`) are intentionally **not** re-exported on the top-level `recost` namespace because they have optional-dep imports. Users access them via `from recost.frameworks.flask import RecostExtension`. The only docstring change here is brand spelling.

- [ ] **Step 2: Run the existing smoke tests to confirm imports still work**

Run: `python -m pytest tests/test_scaffold.py -v`

Expected: all PASS.

Run: `python -c "from recost import init, RecostHandle, RecostConfig; print('ok')"`

Expected: `ok`.

- [ ] **Step 3: Commit**

```bash
git add recost/__init__.py
git commit -m "docs(__init__): standardize on 'Recost' spelling in module docstring

Refs #5"
```

---

## Task 3: Pin the BUILTIN_PROVIDERS count

**Files:**
- Test: `tests/test_provider_registry.py`

- [ ] **Step 1: Read the existing test file header to find the right spot**

Run: `python -m pytest tests/test_provider_registry.py -v --collect-only`

Expected: lists ~30+ test cases. Confirm `class TestBuiltinProviders` or similar exists (or add the new test as a free-standing function — either is fine).

- [ ] **Step 2: Update the stale docstring and add the count assertion**

Find this line in `tests/test_provider_registry.py:14`:

```python
    """Tests for all 21 built-in provider rules."""
```

Replace with:

```python
    """Tests for all 34 built-in provider rules (14 unique providers)."""
```

Then append (at the bottom of the file, before any trailing newline / EOF):

```python
# ---------------------------------------------------------------------------
# Built-in provider count — pins the published claim
# ---------------------------------------------------------------------------

def test_builtin_providers_count_is_pinned():
    """If you add or remove a built-in provider rule, update this assertion
    AND update the provider-count claims in README.md and CLAUDE.md."""
    from recost._provider_registry import BUILTIN_PROVIDERS

    assert len(BUILTIN_PROVIDERS) == 34, (
        f"BUILTIN_PROVIDERS has {len(BUILTIN_PROVIDERS)} rules; "
        f"docs claim 34. Update docs and this assertion together."
    )
```

- [ ] **Step 3: Run the test**

Run: `python -m pytest tests/test_provider_registry.py::test_builtin_providers_count_is_pinned -v`

Expected: PASS (current count is 34).

- [ ] **Step 4: Run the full provider-registry suite**

Run: `python -m pytest tests/test_provider_registry.py -v`

Expected: all tests pass (only the docstring changed in the existing class).

- [ ] **Step 5: Commit**

```bash
git add tests/test_provider_registry.py
git commit -m "test(provider-registry): pin BUILTIN_PROVIDERS count to 34

Adds a regression assertion so adding/removing built-in rules forces
a corresponding docs update. Also corrects the stale '21 built-in
provider rules' docstring.

Refs #5"
```

---

## Task 4: Rewrite `CLAUDE.md` — strip `EcoAPI*`, fix provider count

**Files:**
- Modify: `CLAUDE.md`

`CLAUDE.md` has six concrete problems (verified by grep):

| Line | Current | Why wrong |
|---|---|---|
| 19 | `# Main entry point — wires interceptor, registry, aggregator, transport; returns EcoAPIHandle` | `EcoAPIHandle` doesn't exist; it's `RecostHandle`. |
| 20 | `# All types: RawEvent, MetricEntry, WindowSummary, ProviderDef, EcoAPIConfig, TransportMode` | `EcoAPIConfig` doesn't exist; it's `RecostConfig`. Also missing `FlushStatus`. |
| 21 | `# ProviderRegistry — 21+ built-in rules, wildcard host matching, custom provider priority` | Actual count is 34. |
| 27 | `# EcoAPIMiddleware — ASGI middleware for FastAPI/Starlette` | Class is `RecostMiddleware`. |
| 28 | `# EcoAPI — Flask extension with init_app() pattern` | After Task 1, class is `RecostExtension` (with deprecated `ReCost` alias). |
| 33 | `# All 21 built-in providers, wildcards, Twilio refinement, custom priority` | Actual count is 34. |
| 66 | `21+ built-in rules covering:` | Actual count is 34. |

- [ ] **Step 1: Apply six edits to `CLAUDE.md`**

Edit 1 — line 19:

Find:

```
  _init.py                  # Main entry point — wires interceptor, registry, aggregator, transport; returns EcoAPIHandle
```

Replace with:

```
  _init.py                  # Main entry point — wires interceptor, registry, aggregator, transport; returns RecostHandle
```

Edit 2 — line 20:

Find:

```
  _types.py                 # All types: RawEvent, MetricEntry, WindowSummary, ProviderDef, EcoAPIConfig, TransportMode
```

Replace with:

```
  _types.py                 # All types: RawEvent, MetricEntry, WindowSummary, ProviderDef, RecostConfig, FlushStatus, TransportMode
```

Edit 3 — line 21:

Find:

```
  _provider_registry.py     # ProviderRegistry — 21+ built-in rules, wildcard host matching, custom provider priority
```

Replace with:

```
  _provider_registry.py     # ProviderRegistry — 34 built-in rules (14 providers), wildcard host matching, custom provider priority
```

Edit 4 — line 27:

Find:

```
    fastapi.py              # EcoAPIMiddleware — ASGI middleware for FastAPI/Starlette
```

Replace with:

```
    fastapi.py              # RecostMiddleware — ASGI middleware for FastAPI/Starlette
```

Edit 5 — line 28:

Find:

```
    flask.py                # EcoAPI — Flask extension with init_app() pattern
```

Replace with:

```
    flask.py                # RecostExtension — Flask extension with init_app() pattern (ReCost is a deprecated alias)
```

Edit 6 — line 33:

Find:

```
  test_provider_registry.py # All 21 built-in providers, wildcards, Twilio refinement, custom priority
```

Replace with:

```
  test_provider_registry.py # All 34 built-in provider rules, wildcards, Twilio refinement, custom priority
```

Edit 7 — line 66:

Find:

```
21+ built-in rules covering:
```

Replace with:

```
34 built-in rules across 14 providers:
```

- [ ] **Step 2: Verify all stale references are gone**

Run: `python -m grep -nE "EcoAPI|21\+? built-in|21 built-in" CLAUDE.md` (or use ripgrep / VS Code search).

Expected: no matches.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(CLAUDE): strip nonexistent EcoAPI* names; fix provider count

CLAUDE.md referenced EcoAPIHandle, EcoAPIConfig, EcoAPIMiddleware, and
EcoAPI — none of which exist in the codebase. Replace with the actual
class names (RecostHandle, RecostConfig, RecostMiddleware,
RecostExtension). Correct the provider-rule count from '21+' to '34'.

Refs #5"
```

---

## Task 5: Update `README.md` — Flask example, config table

**Files:**
- Modify: `README.md`

Three concrete problems in `README.md` (verified by grep + read):

1. Lines 84, 87, 93 reference the `ReCost` class — update to `RecostExtension`.
2. Line 106 documents `flush_interval` (deprecated seconds option) as the canonical option — replace with `flush_interval_ms` and add a row for the deprecated form.
3. `max_buckets` and `shutdown_flush_timeout_ms` are not documented at all.

- [ ] **Step 1: Update the Flask example**

Find (in `README.md`, around lines 80–95):

```
### Flask

```python
from flask import Flask
from recost.frameworks.flask import ReCost

app = Flask(__name__)
ReCost(app, api_key="...", project_id="...")
```

Or using the `init_app` pattern:

```python
recost = ReCost()
recost.init_app(app, api_key="...", project_id="...")
```
```

Replace with:

````
### Flask

```python
from flask import Flask
from recost.frameworks.flask import RecostExtension

app = Flask(__name__)
RecostExtension(app, api_key="...", project_id="...")
```

Or using the `init_app` pattern:

```python
ext = RecostExtension()
ext.init_app(app, api_key="...", project_id="...")
```

> **Note:** the old class name `ReCost` is still importable as a deprecated
> alias and will continue to work for one release with a `DeprecationWarning`.
> Migrate to `RecostExtension`.
````

- [ ] **Step 2: Update the config table**

Find (in `README.md`, lines 101–115):

```
| Option | Type | Default | Description |
|---|---|---|---|
| `api_key` | `str` | — | Recost API key (`rc-...`). If omitted, runs in local mode. |
| `project_id` | `str` | — | Recost project ID. Required in cloud mode. |
| `environment` | `str` | `"development"` | Environment tag attached to all telemetry. |
| `flush_interval` | `float` | `30.0` | Seconds between automatic flushes. |
| `max_batch_size` | `int` | `100` | Early-flush threshold (number of events). |
| `local_port` | `int` | `9847` | WebSocket port for the VS Code extension. |
| `debug` | `bool` | `False` | Log telemetry activity to stderr. |
| `enabled` | `bool` | `True` | Master kill switch — set `False` to disable entirely. |
| `custom_providers` | `list[ProviderDef]` | `[]` | Extra provider rules with higher priority than built-ins. |
| `exclude_patterns` | `list[str]` | `[]` | URL substrings — matching requests are silently dropped. |
| `base_url` | `str` | `"https://api.recost.dev"` | Override for self-hosted deployments. |
| `max_retries` | `int` | `3` | Retry attempts for failed cloud flushes. |
| `on_error` | `Callable` | — | Called on internal SDK errors. |
```

Replace with:

```
| Option | Type | Default | Description |
|---|---|---|---|
| `api_key` | `str` | — | Recost API key (`rc-...`). If omitted, runs in local mode. |
| `project_id` | `str` | — | Recost project ID. Required in cloud mode. |
| `environment` | `str` | `"development"` | Environment tag attached to all telemetry. |
| `flush_interval_ms` | `int` | `30000` | Milliseconds between automatic aggregator flushes. |
| `flush_interval` | `float` | — | **Deprecated.** Legacy seconds-based flush interval. If set, takes precedence over `flush_interval_ms` and emits a `DeprecationWarning`. Will be removed in a future release. |
| `max_batch_size` | `int` | `100` | Early-flush threshold (number of events). |
| `max_buckets` | `int` | `2000` | Maximum unique (provider, endpoint, method) triplets per window. Crossing this triggers an early flush. |
| `local_port` | `int` | `9847` | WebSocket port for the VS Code extension. |
| `debug` | `bool` | `False` | Log telemetry activity to stderr. |
| `enabled` | `bool` | `True` | Master kill switch — set `False` to disable entirely. |
| `custom_providers` | `list[ProviderDef]` | `[]` | Extra provider rules with higher priority than built-ins. |
| `exclude_patterns` | `list[str]` | `[]` | URL substrings — matching requests are silently dropped. |
| `base_url` | `str` | `"https://api.recost.dev"` | Override for self-hosted deployments. |
| `max_retries` | `int` | `3` | Retry attempts for failed cloud flushes. |
| `shutdown_flush_timeout_ms` | `int` | `3000` | How long `dispose()` waits for the final flush to complete before closing the transport. |
| `on_error` | `Callable[[Exception], None]` | — | Called on internal SDK errors. |
```

- [ ] **Step 3: Verify no stale `ReCost` or `flush_interval` (without `_ms`) references remain in code-block examples**

Run: `python -m grep -n "ReCost\|EcoAPI" README.md` (PowerShell: `Select-String -Path README.md -Pattern "ReCost|EcoAPI"`).

Expected: only matches inside the deprecation note added in Step 1.

Run: `python -m grep -n "flush_interval" README.md`.

Expected: matches are the two new table rows (`flush_interval_ms` and the deprecated `flush_interval`), nothing else.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs(README): use RecostExtension; document real config fields

- Flask example switches to RecostExtension (with a note about the
  deprecated ReCost alias).
- Config table swaps the stale 'flush_interval' (seconds, deprecated)
  for the canonical 'flush_interval_ms' (milliseconds), with a separate
  row marking the old form deprecated.
- Adds previously-undocumented options: max_buckets,
  shutdown_flush_timeout_ms.

Refs #5"
```

---

## Task 6: Final verification — full suite + open the PR

- [ ] **Step 1: Run the full test suite**

Run: `python -m pytest`

Expected: every test passes (130+ existing, plus the new deprecation tests and the count-pinning assertion).

- [ ] **Step 2: Verify the deprecation alias actually warns**

Run: `python -c "from flask import Flask; from recost.frameworks.flask import ReCost; ReCost(Flask(__name__), enabled=False)" 2>&1`

Expected: output contains `DeprecationWarning: recost.frameworks.flask.ReCost is deprecated ...`.

(If `flask` is not installed in the dev environment, install it: `python -m pip install -e ".[flask]"`.)

- [ ] **Step 3: Run ruff and mypy on the touched files**

Run: `python -m ruff check recost/frameworks/flask.py recost/__init__.py tests/test_flask.py tests/test_provider_registry.py`

Expected: clean.

Run: `python -m mypy recost/frameworks/flask.py`

Expected: no new errors. (Pre-existing mypy errors elsewhere are tracked by issue #2 and out of scope here.)

- [ ] **Step 4: Push and open the PR**

```bash
git push -u origin <branch-name>
gh pr create --title "docs+rename: unify naming and reconcile docs (closes #5)" --body "$(cat <<'EOF'
## Summary

Closes #5.

- Renames the Flask extension class `ReCost` → `RecostExtension` to match
  the convention used by `RecostMiddleware` (FastAPI). `ReCost` remains
  importable as a thin subclass that emits a `DeprecationWarning`.
- Strips every reference to nonexistent `EcoAPI*` names from `CLAUDE.md`.
- Replaces the stale `flush_interval` (seconds, deprecated) row in the
  README config table with `flush_interval_ms` (canonical) and a
  separate deprecated row for `flush_interval`.
- Documents previously-undocumented config fields: `max_buckets`,
  `shutdown_flush_timeout_ms`.
- Pins `len(BUILTIN_PROVIDERS) == 34` via a regression test so future
  drift trips CI.

## Tests

- `TestRecostExtension` — existing Flask tests, updated to the new name.
- `TestReCostDeprecationAlias::test_old_name_still_constructs` — proves
  the deprecation alias emits a warning on construction.
- `TestReCostDeprecationAlias::test_old_name_is_subclass_or_alias_of_new`
  — proves `isinstance` checks keep working.
- `test_builtin_providers_count_is_pinned` — pins the documented count.

## Migration

External consumers using `from recost.frameworks.flask import ReCost`
will see a `DeprecationWarning` but no behavioral change. Switch to
`from recost.frameworks.flask import RecostExtension` before the next
major release.
EOF
)"
```

---

## Self-review

- **Spec coverage:** Issue #5 requires (1) pick one brand spelling — done, `Recost`; (2) rename `ReCost` → `RecostExtension` with deprecation alias — Task 1; (3) strip `EcoAPI*` from `CLAUDE.md` — Task 4; (4) update README provider count, flush_interval, missing fields — Task 5; (5) add `len(BUILTIN_PROVIDERS) == 34` test — Task 3. All five accounted for.
- **Placeholder scan:** No TBDs. Every Find/Replace shows exact text. Every test has full body.
- **Type consistency:** `RecostExtension` defined in Task 1, referenced consistently in Task 2 (`__init__.py`), Task 4 (CLAUDE.md description), Task 5 (README example), Task 6 (PR body). `RecostHandle` and `RecostConfig` are imported from `_init`/`_types` and unchanged.
- **Dependencies on other issues:** None. Worktree created from `main`. No conflict with the aggregator plan (different files).
