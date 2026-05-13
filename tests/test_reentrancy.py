"""Regression test for issue #15 — reentrancy guard must work across thread hops."""

import threading
from typing import List
from unittest.mock import MagicMock, patch


def test_nested_patch_does_not_double_record() -> None:
    """Simulate the failure mode: an outer task contextvar is set (mimicking
    an outer patch firing), work hops to a thread (contextvar reset in that thread),
    and the inner urllib3 patch fires. With a thread-local guard, the inner
    patch must see the guard and skip."""
    from recost._interceptor import install, uninstall, _in_interceptor_task

    events: List = []
    install(events.append)
    try:
        token = _in_interceptor_task.set(True)
        try:
            with patch("urllib3.HTTPConnectionPool._make_request") as mock_req:
                mock_resp = MagicMock(status=200, headers={"content-length": "0"})
                mock_req.return_value = mock_resp
                import requests

                def call_in_thread() -> None:
                    try:
                        requests.get("http://example.invalid/")
                    except Exception:
                        pass  # connection failure expected; we only care about event count

                t = threading.Thread(target=call_in_thread)
                t.start()
                t.join()

            assert len(events) == 0, (
                f"Thread saw a fresh context and double-recorded; got {len(events)} events"
            )
        finally:
            _in_interceptor_task.reset(token)
    finally:
        uninstall()
