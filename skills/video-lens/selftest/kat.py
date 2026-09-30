#!/usr/bin/env python3
"""Known-answer test runner: imports every selftest/kat_*.py, runs the selected KATs, prints a
PASS/FAIL/SKIP table and returns 1 when anything failed.

    python3 kat.py [--quick] [--only ID,ID] [--chrome] [--keep]     (same as `vl.py selftest ...`)
"""
import argparse
import importlib
import os
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import katlib  # noqa: E402
from katlib import FAIL, PASS, SKIP, Ctx  # noqa: E402
from vl.cache import CACHE_DIR_ENV  # noqa: E402

ROW = "{:<7} {:<8} {:<4} {:>6}  {}"


def print_row(kat_id, area, status, secs, detail):
    print(ROW.format(kat_id, area, status, secs, detail), flush=True)


def discover():
    """Imports kat_*.py modules; a module that fails to import is reported as a FAIL row, not a crash."""
    broken = []
    for path in sorted(katlib.SELFTEST_DIR.glob("kat_*.py")):
        try:
            importlib.import_module(path.stem)
        except Exception as error:
            broken.append((path.stem, f"import failed: {type(error).__name__}: {error}"))
    return broken


def select(kats, is_quick, only):
    if only:
        known = {k.id for k in kats}
        return [k for k in kats if k.id in only], [kat_id for kat_id in only if kat_id not in known]
    return [k for k in kats if not k.is_manual and (k.is_quick or not is_quick)], []


def run_one(entry, ctx):
    if entry.needs_chrome and not ctx.is_chrome:
        return SKIP, "needs --chrome"
    try:
        status, detail = entry.fn(ctx)
    except Exception as error:
        frame = traceback.extract_tb(error.__traceback__)[-1]
        return FAIL, f"{type(error).__name__}: {error} at {Path(frame.filename).name}:{frame.lineno}"
    if status not in (PASS, FAIL, SKIP):
        return FAIL, f"KAT returned status {status!r}"
    return status, detail


def run_selftest(quick=False, only=None, chrome=False, keep=False):
    work = Path(tempfile.mkdtemp(prefix="video-lens-selftest-"))
    os.environ[CACHE_DIR_ENV] = str(work / "cache")
    ctx = Ctx(work, is_quick=quick, is_chrome=chrome)
    broken = discover()
    chosen, unknown = select(katlib.REGISTRY, quick, only)
    counts = {PASS: 0, FAIL: 0, SKIP: 0}
    started = time.monotonic()
    print_row("ID", "AREA", "STAT", "SECS", "DETAIL")
    for module, message in broken:
        status = SKIP if only else FAIL     # with --only, another builder's half-written module is not this run's failure
        counts[status] += 1
        print_row("IMPORT", module, status, "-", message)
    for kat_id in unknown:
        counts[FAIL] += 1
        print_row(kat_id, "-", FAIL, "-", "unknown KAT id")
    for entry in chosen:
        kat_started = time.monotonic()
        status, detail = run_one(entry, ctx)
        counts[status] += 1
        print_row(entry.id, entry.area, status, f"{time.monotonic() - kat_started:.1f}", detail)
    print(f"PASS {counts[PASS]} · FAIL {counts[FAIL]} · SKIP {counts[SKIP]} · {time.monotonic() - started:.1f} s", flush=True)
    if keep:
        print(f"kept: {work}")
    else:
        shutil.rmtree(work, ignore_errors=True)
    return 1 if counts[FAIL] else 0


def main():
    parser = argparse.ArgumentParser(description="video-lens known-answer tests")
    parser.add_argument("--quick", action="store_true", help="only rows marked Q")
    parser.add_argument("--only", type=lambda s: [p.strip() for p in s.split(",") if p.strip()], help="ID,ID")
    parser.add_argument("--chrome", action="store_true", help="also run the Chrome KATs")
    parser.add_argument("--keep", action="store_true", help="keep fixtures and outputs")
    args = parser.parse_args()
    return run_selftest(args.quick, args.only, args.chrome, args.keep)


if __name__ == "__main__":
    sys.exit(main())
