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
