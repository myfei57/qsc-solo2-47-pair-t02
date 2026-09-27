"""Tiny test runner backed by tests/_pytest_shim.py.

Usage: python3 run_tests.py
Mirrors ``pytest -q`` closely enough for this suite: collects ``test_*``
callables, resolves fixtures from tests/conftest.py (recursively) and prints
dots/Fs with a summary. Exit code is non-zero on any failure.
"""

import importlib.util
import inspect
import os
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TESTS = ROOT / "tests"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS))

# Install the shim as ``pytest`` before test modules import it.
sys.modules["pytest"] = importlib.import_module("_pytest_shim")


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_fixtures() -> dict:
    conftest = load_module(TESTS / "conftest.py", "conftest")
    fixtures = {}
    for name, obj in vars(conftest).items():
        if callable(obj) and getattr(obj, "__pytest_fixture__", False):
            fixtures[name] = obj
    # pytest built-ins used by the suite
    fixtures["tmp_path"] = lambda: Path(tempfile.mkdtemp(prefix="potted-"))
    return fixtures


def resolve(name, fixtures, cache):
    if name in cache:
        return cache[name]
    if name not in fixtures:
        raise KeyError(f"no fixture named {name!r}")
    factory = fixtures[name]
    kwargs = {}
    for dependency in inspect.signature(factory).parameters:
        kwargs[dependency] = resolve(dependency, fixtures, cache)
    value = factory(**kwargs)
    cache[name] = value
    return value


def main() -> int:
    fixtures = load_fixtures()
    test_files = sorted(TESTS.glob("test_*.py"))
    total = failures = 0
    failed_names = []

    for path in test_files:
        module = load_module(path, f"suite_{path.stem}")
        tests = [
            (name, obj)
            for name, obj in inspect.getmembers(module, inspect.isfunction)
            if name.startswith("test_") and obj.__module__ == module.__name__
        ]
        for name, test in sorted(tests):
            total += 1
            cache = {}
            try:
                kwargs = {
                    param: resolve(param, fixtures, cache)
                    for param in inspect.signature(test).parameters
                }
                test(**kwargs)
                print(".", end="", flush=True)
            except Exception:  # noqa: BLE001 - report every failure
                failures += 1
                failed_names.append((f"{path.name}::{name}", traceback.format_exc()))
                print("F", end="", flush=True)

    print()
    for name, formatted in failed_names:
        print("=" * 70)
        print("FAIL", name)
        print(formatted.rstrip())
    print(f"\n{total} tests, {failures} failures")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
