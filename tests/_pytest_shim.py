"""Minimal pytest-compatible shim.

The sandbox has no package index and no pytest, so this module provides just
enough of the pytest API used by the test suite: ``raises`` and ``fixture``,
plus a tiny runner (see ``run_tests.py``) that resolves the fixtures declared
in tests/conftest.py.
"""

import contextlib
import functools
import inspect


def raises(expected_exception, match=None):
    class _Checker:
        def __init__(self):
            self.value = None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            if exc_type is None:
                raise AssertionError(f"expected {expected_exception.__name__}, nothing raised")
            if not issubclass(exc_type, expected_exception):
                return False
            self.value = exc
            if match is not None:
                import re

                if not re.search(match, str(exc)):
                    raise AssertionError(f"{exc!r} did not match {match!r}")
            return True

    return _Checker()


def fixture(*args, **kwargs):
    """Mark a function as a fixture; supports bare @fixture and @fixture()."""
    if args and callable(args[0]) and not kwargs:
        function = args[0]
        function.__pytest_fixture__ = True
        return function

    def decorator(function):
        function.__pytest_fixture__ = True
        return function

    return decorator


def mark():
    raise NotImplementedError("pytest.mark is not used by this suite")
