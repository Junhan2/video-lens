"""Swift helpers are compiled on first use into ~/.cache/video-lens/bin/<name>-<sha8 of source> (spec 4)."""
import fcntl
import hashlib
import os
import subprocess
from pathlib import Path

from . import SWIFT_DIR
from .cache import BIN_DIR
from .errors import EXIT_DEPENDENCY, VlError

SWIFTC = "/usr/bin/swiftc"
BUILD_FLAGS = ("-O", "-parse-as-library")   # sources use @main


def helper_path(name):
    """Executable for skill/swift/<name>.swift, compiled now if this exact source has not been built yet."""
    return build_swift(SWIFT_DIR / f"{name}.swift", name)


def build_swift(source, name, bin_dir=BIN_DIR):
    source = Path(source)
    if not source.is_file():
        raise VlError(EXIT_DEPENDENCY, f"swift helper source missing: {source}", "Reinstall the video-lens skill")
    target = Path(bin_dir) / f"{name}-{hashlib.sha256(source.read_bytes()).hexdigest()[:8]}"
    if os.access(target, os.X_OK):
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target.parent / f".{name}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not os.access(target, os.X_OK):     # a parallel process may have built it while this one waited
            compile_to(source, target)
            remove_stale_builds(target, name)
    return target


def compile_to(source, target):
    if not os.access(SWIFTC, os.X_OK):
        raise VlError(EXIT_DEPENDENCY, f"swiftc not found at {SWIFTC}", "Install the Xcode command line tools (xcode-select --install)")
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    result = subprocess.run([SWIFTC, *BUILD_FLAGS, "-o", str(tmp), str(source)], capture_output=True, text=True)
    if result.returncode != 0 or not tmp.is_file():
        tmp.unlink(missing_ok=True)
        errors = [line for line in result.stderr.splitlines() if "error:" in line] or [f"swiftc exit {result.returncode}"]
        raise VlError(EXIT_DEPENDENCY, f"swift helper {source.stem} failed to build: {errors[0].strip()}",
                      "Run xcode-select --install, then retry")
    os.replace(tmp, target)


def remove_stale_builds(target, name):
    for old in target.parent.glob(f"{name}-*"):
        if old != target and len(old.name) == len(target.name):
            old.unlink(missing_ok=True)
