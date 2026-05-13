"""Tests for the typed-error hierarchy exposed by recost."""


def test_recost_error_is_importable() -> None:
    from recost import RecostError
    assert issubclass(RecostError, Exception)


def test_recost_error_carries_message() -> None:
    from recost import RecostError
    err = RecostError("something broke")
    assert str(err) == "something broke"


def test_auth_error_carries_status_and_count() -> None:
    from recost import RecostError, RecostAuthError
    err = RecostAuthError(status=401, consecutive_failures=3)
    assert isinstance(err, RecostError)
    assert err.status == 401
    assert err.consecutive_failures == 3
    assert "401" in str(err)


def test_fatal_auth_error_is_an_auth_error() -> None:
    from recost import RecostAuthError, RecostFatalAuthError
    err = RecostFatalAuthError(status=401, consecutive_failures=5)
    assert isinstance(err, RecostAuthError)
    assert err.consecutive_failures == 5


def test_rate_limit_error_carries_retry_after() -> None:
    from recost import RecostError, RecostRateLimitError
    err = RecostRateLimitError(retry_after_ms=2500, endpoint="/projects/p_1/telemetry")
    assert isinstance(err, RecostError)
    assert err.retry_after_ms == 2500
    assert err.endpoint == "/projects/p_1/telemetry"
    assert "2500" in str(err)
