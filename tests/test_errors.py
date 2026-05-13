"""Tests for the typed-error hierarchy exposed by recost."""


def test_recost_error_is_importable() -> None:
    from recost import RecostError
    assert issubclass(RecostError, Exception)


def test_recost_error_carries_message() -> None:
    from recost import RecostError
    err = RecostError("something broke")
    assert str(err) == "something broke"
