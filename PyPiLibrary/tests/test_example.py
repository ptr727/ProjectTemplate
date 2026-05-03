"""Tests for ``ptr727_projecttemplate_library.example``."""

from ptr727_projecttemplate_library import __version__, greet


def test_version_is_string() -> None:
    assert isinstance(__version__, str)
    assert len(__version__) > 0


def test_greet_uses_name() -> None:
    assert greet("world") == "Hello, world!"


def test_greet_with_empty_name() -> None:
    assert greet("") == "Hello, !"
