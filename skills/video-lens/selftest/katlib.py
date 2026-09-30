"""KAT registry and helpers shared by the runner (kat.py) and every selftest/kat_*.py module.

A KAT is a function fn(ctx) -> (status, detail), registered with the @kat decorator:

    from katlib import kat, PASS, FAIL, SKIP

    @kat("C5", "io", quick=True)
    def showinfo_matches_cv2(ctx):
        ...
        return PASS, "947/947 frames, max |dt| 0.02 ms"
"""
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

SELFTEST_DIR = Path(__file__).resolve().parent
SKILL_DIR = SELFTEST_DIR.parent
SCRIPTS_DIR = SKILL_DIR / "scripts"
VL_PY = SCRIPTS_DIR / "vl.py"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


@dataclass(frozen=True)
class Kat:
    id: str
    area: str
    fn: Callable
    is_quick: bool          # row marked Q in spec 10: runs under --quick
    needs_chrome: bool      # SKIP unless --chrome
    is_manual: bool         # runs only when named with --only


REGISTRY = []


def kat(kat_id, area, *, quick=False, chrome=False, manual=False):
    def register(fn):
        REGISTRY.append(Kat(kat_id, area, fn, quick, chrome, manual))
        return fn
    return register


class Ctx:
    """What a KAT gets: a scratch folder for this selftest run, memoised fixtures and a vl.py runner.

    VIDEO_LENS_CACHE_DIR points into the scratch folder, so KATs never touch the user's cache.
    """

    def __init__(self, work, is_quick=False, is_chrome=False):
        self.work = Path(work)
        self.is_quick = is_quick
        self.is_chrome = is_chrome
        self.fixture_dir = self.work / "fixtures"
        self.fixture_dir.mkdir(parents=True, exist_ok=True)

    def fixture(self, name, build):
        """Path of fixture `name`, built once per selftest run by build(tmp_path); tmp keeps the extension."""
        path = self.fixture_dir / name
        if not path.exists():
            tmp = path.with_name(f".tmp-{name}")
            build(tmp)
            os.replace(tmp, path)
        return path

    def out_dir(self, name):
        path = self.work / "out" / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def vl(self, *args, timeout=900):
        """Runs `python3 vl.py ARGS` as the user would; returns the CompletedProcess (text mode)."""
        return subprocess.run([sys.executable, str(VL_PY), *map(str, args)], capture_output=True, text=True,
                              timeout=timeout, env=os.environ.copy())


def verdict(failures, detail):
    """PASS with `detail` when the failure list is empty, else FAIL listing every failure."""
    return (FAIL, "; ".join(failures)) if failures else (PASS, detail)
