"""Regression test for issue #15 — reentrancy guard must work across thread hops
that go through asyncio's contextvar-propagating mechanisms (asyncio.to_thread,
loop.run_in_executor)."""

import asyncio
from typing import List
from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_asyncio_to_thread_does_not_double_count() -> None:
    """When an async task with the guard active dispatches sync work via
    asyncio.to_thread, asyncio propagates the ContextVar into the worker
    thread, so a re-entered urllib3 patch must see the guard and skip."""
    from recost._interceptor import install, uninstall, _enter_interceptor, _exit_interceptor

    events: List = []
    install(events.append)
    try:
        # Establish guard-active state in this async task
        token = _enter_interceptor()
        try:
            with patch("urllib3.HTTPConnectionPool._make_request") as mock_req:
                mock_resp = MagicMock(status=200, headers={"content-length": "0"})
                mock_req.return_value = mock_resp
                import requests

                def call_sync() -> None:
                    try:
                        requests.get("http://example.invalid/")
                    except Exception:
                        pass  # connection failure expected; we only care about event count

                # asyncio.to_thread propagates contextvars to the worker thread
                await asyncio.to_thread(call_sync)
            assert len(events) == 0, (
                f"Worker thread didn't inherit guard ContextVar; got {len(events)} events"
            )
        finally:
            _exit_interceptor(token)
    finally:
        uninstall()


def test_same_thread_recursion_does_not_double_count() -> None:
    """Within a single thread, the threading.local flag prevents recursive entry."""
    from recost._interceptor import install, uninstall, _enter_interceptor, _exit_interceptor, _is_in_interceptor

    events: List = []
    install(events.append)
    try:
        assert not _is_in_interceptor()
        token = _enter_interceptor()
        try:
            assert _is_in_interceptor()
        finally:
            _exit_interceptor(token)
        assert not _is_in_interceptor()
    finally:
        uninstall()
