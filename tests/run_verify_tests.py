import inspect
import importlib.util
import os
import sys
import traceback
from pathlib import Path
from tempfile import TemporaryDirectory


def load_module_from_path(path: Path):
    name = path.stem
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot import {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_tests():
    tests_dir = Path(__file__).resolve().parents[1] / "tests"
    # By default run only verification-related tests to avoid importing
    # unrelated project modules that may require extra dependencies.
    default = [
        tests_dir / "tests_verify.py",
        tests_dir / "test_verifier_adapter.py",
    ]
    env = os.getenv("TEST_FILES")
    if env:
        names = [n.strip() for n in env.split(",") if n.strip()]
        test_files = [tests_dir / n for n in names]
    else:
        test_files = [p for p in default if p.exists()]
    funcs = []
    for f in test_files:
        mod = load_module_from_path(f)
        funcs.extend([getattr(mod, n) for n in dir(mod) if n.startswith("test_")])

    total = len(funcs)
    passed = 0

    for fn in funcs:
        name = fn.__name__
        try:
            sig = inspect.signature(fn)
            if "tmp_path" in sig.parameters:
                with TemporaryDirectory() as td:
                    tmp = Path(td)
                    fn(tmp)
            else:
                fn()
            passed += 1
            print(f"PASS: {name}")
        except AssertionError:
            tb = traceback.format_exc()
            print(f"FAIL: {name}\n{tb}")
        except Exception:
            tb = traceback.format_exc()
            print(f"ERROR: {name}\n{tb}")

    print(f"\nSummary: {passed}/{total} tests passed")
    if passed != total:
        sys.exit(1)


if __name__ == "__main__":
    run_tests()
