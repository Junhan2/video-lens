"""Fingerprint cache (spec 5.5): key, stage files with param hashes, fcntl lock, LRU cap, purge, URL downloads."""
import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import numpy as np

from .errors import EXIT_DEPENDENCY, EXIT_INPUT, VlError

CACHE_DIR_ENV = "VIDEO_LENS_CACHE_DIR"      # relocates stage caches and downloads (selftest isolation)
DEFAULT_ROOT = Path.home() / ".cache" / "video-lens"
BIN_DIR = DEFAULT_ROOT / "bin"              # compiled swift helpers; keyed by source hash, never purged
HEAD_TAIL_BYTES = 1 << 20
LRU_CAP_BYTES = 2 * 1024 ** 3
STAGE_FORMAT = "v1"
KEY_PATTERN = re.compile(r"^[0-9a-f]{16}$")
PARTIAL_DOWNLOAD_SUFFIXES = (".part", ".ytdl", ".temp")
META_NAME = "meta.json"
PACKETS_NAME = "packets.v1.npy"


def cache_root():
    return Path(os.environ.get(CACHE_DIR_ENV) or DEFAULT_ROOT)


def file_key(path):
    """sha1(size || sha1(first 1 MiB) || sha1(last 1 MiB))[:16]: survives rename and copy, reads at most 2 MiB."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        head = f.read(HEAD_TAIL_BYTES)
        f.seek(max(0, size - HEAD_TAIL_BYTES))
        tail = f.read(HEAD_TAIL_BYTES)
    digest = hashlib.sha1(str(size).encode())
    digest.update(hashlib.sha1(head).digest())
    digest.update(hashlib.sha1(tail).digest())
    return digest.hexdigest()[:16]


def params_hash(params):
    text = json.dumps(params, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(text.encode()).hexdigest()[:8]


def write_atomic(path, data):
    """Write bytes via a temp file in the same folder, so a killed run never leaves a half-written file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class Cache:
    """One input's folder `<root>/<key16>/`. Records stage hits and misses for analysis.json."""

    def __init__(self, key, root=None, is_refresh=False):
        self.key = key
        self.root = Path(root) if root else cache_root()
        self.dir = self.root / key
        self.dir.mkdir(parents=True, exist_ok=True)
        self.is_refresh = is_refresh
        self.hits = []
        self.misses = []
        self.fresh_frames = set()       # frame files written by this run: --refresh re-exports each one once

    def entry(self, stage, params, ext, version=STAGE_FORMAT):
        """Stage file `<stage>.<param-hash8>.<version>.<ext>`; a hit when it exists and --refresh is off."""
        return CacheEntry(self, stage, self.dir / f"{stage}.{params_hash(params)}.{version}.{ext}")

    def record(self, stage, is_hit):
        target, other = (self.hits, self.misses) if is_hit else (self.misses, self.hits)
        if stage not in target and stage not in other:
            target.append(stage)

    @property
    def frames_dir(self):
        path = self.dir / "frames"
        path.mkdir(exist_ok=True)
        return path

    @contextmanager
    def locked(self):
        """Exclusive fcntl lock on `<key>/lock` for the whole analyze run; a second run on the same file waits."""
        with open(self.dir / "lock", "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def read_meta(self):
        try:
            return json.loads((self.dir / META_NAME).read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def write_meta(self, meta):
        write_atomic(self.dir / META_NAME, json.dumps(meta, ensure_ascii=False, indent=1).encode())

    def load_probe(self, versions):
        """Cached (probe info, packet times) when meta was written by the same tool versions, else None."""
        meta = self.read_meta()
        packets = self.dir / PACKETS_NAME
        if self.is_refresh or meta.get("versions") != versions or "probe" not in meta or not packets.is_file():
            return None
        return meta["probe"], np.load(packets)

    def save_probe(self, info, packet_times, versions):
        """Stores the probe; stage files made by other tool versions are dropped first."""
        if self.read_meta().get("versions") not in (None, versions):
            self.clear_stage_files()
        buffer = io.BytesIO()
        np.save(buffer, packet_times)
        write_atomic(self.dir / PACKETS_NAME, buffer.getvalue())
        self.write_meta({"key": self.key, "input_path": info["path"], "size_bytes": info["size_bytes"],
                         "versions": versions, "probe": info, "last_used": time.time()})

    def touch(self):
        meta = self.read_meta()
        if meta:
            meta["last_used"] = time.time()
            self.write_meta(meta)

    def clear_stage_files(self):
        for child in self.dir.iterdir():
            if child.name in ("lock", META_NAME):
                continue
            shutil.rmtree(child) if child.is_dir() else child.unlink()


class CacheEntry:
    """A stage file. Whole-file stages use load_*/save_*; resumable stages append to `<path>.part` and finish it."""

    def __init__(self, cache, stage, path):
        self.stage = stage
        self.path = path
        self.partial_path = path.with_name(path.name + ".part")
        self.is_hit = path.is_file() and not cache.is_refresh
        if cache.is_refresh:
            self.partial_path.unlink(missing_ok=True)
        cache.record(stage, self.is_hit)

    def load_json(self):
        return json.loads(self.path.read_text())

    def save_json(self, obj):
        write_atomic(self.path, json.dumps(obj, ensure_ascii=False, default=json_default).encode())

    def load_npz(self):
        with np.load(self.path, allow_pickle=False) as data:
            return {name: data[name] for name in data.files}

    def save_npz(self, **arrays):
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".npz")
        os.close(fd)
        try:
            np.savez(tmp, **arrays)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def read_jsonl(self):
        return parse_jsonl(self.path.read_text())

    def save_jsonl(self, rows):
        write_atomic(self.path, "".join(jsonl_line(row) for row in rows).encode())

    def read_partial(self):
        """Rows already appended by an earlier, possibly killed, run; a torn last line is ignored."""
        if not self.partial_path.is_file():
            return []
        return parse_jsonl(self.partial_path.read_text())

    def append_partial(self, rows):
        with open(self.partial_path, "a", encoding="utf-8") as f:
            f.write("".join(jsonl_line(row) for row in rows))
            f.flush()
            os.fsync(f.fileno())

    def finish_partial(self):
        """Drops a torn tail line, then promotes `.part` to the final stage file."""
        rows = self.read_partial()
        self.partial_path.unlink(missing_ok=True)
        self.save_jsonl(rows)


def jsonl_line(row):
    return json.dumps(row, ensure_ascii=False, default=json_default) + "\n"


def parse_jsonl(text):
    rows = []
    for line in text.splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def json_default(value):
    """numpy scalars and arrays inside results serialise as plain JSON numbers and lists."""
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"{type(value).__name__} is not JSON serialisable")


def cache_items(root):
    """Purgeable items: key folders and downloaded files, as (last_used, size_bytes, path)."""
    items = []
    if not root.is_dir():
        return items
    for child in root.iterdir():
        if child.is_dir() and KEY_PATTERN.match(child.name):
            meta = child / META_NAME
            items.append(((meta if meta.exists() else child).stat().st_mtime, tree_size(child), child))
    downloads = root / "downloads"
    if downloads.is_dir():
        items += [(f.stat().st_mtime, f.stat().st_size, f) for f in downloads.iterdir() if f.is_file()]
    return items


def tree_size(path):
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def is_in_use(key_dir):
    lock = key_dir / "lock"
    if not lock.exists():
        return False
    with open(lock, "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
    return False


def remove_item(path):
    shutil.rmtree(path) if path.is_dir() else path.unlink()


def enforce_lru(root, keep, cap_bytes=LRU_CAP_BYTES):
    """Deletes least recently used items until the cache fits cap_bytes. `keep` paths and locked folders stay."""
    items = sorted(cache_items(root))
    total = sum(size for _, size, _ in items)
    removed = []
    keep = {Path(p).resolve() for p in keep}
    for _, size, path in items:
        if total <= cap_bytes:
            break
        if path.resolve() in keep or (path.is_dir() and is_in_use(path)):
            continue
        remove_item(path)
        total -= size
        removed.append(path)
    return removed


def purge_older(root, days, keep=()):
    cutoff = time.time() - days * 86400
    keep = {Path(p).resolve() for p in keep}
    removed = []
    for last_used, _, path in cache_items(root):
        if last_used >= cutoff or path.resolve() in keep or (path.is_dir() and is_in_use(path)):
            continue
        remove_item(path)
        removed.append(path)
    return removed


def list_entries(root):
    """Rows for `vl.py cache --list`: key, size, last use, the input path seen last."""
    rows = []
    for last_used, size, path in sorted(cache_items(root), reverse=True):
        name = path.name
        if path.is_dir():
            try:
                name = json.loads((path / META_NAME).read_text()).get("input_path", name)
            except (FileNotFoundError, json.JSONDecodeError):
                pass
        rows.append({"item": path.name, "size_bytes": size, "last_used": last_used, "input": name})
    return rows


def download_url(url, root=None):
    """yt-dlp download (<= 1080p) to `<root>/downloads/<sha1(url)[:16]>.<ext>`, reused when already present."""
    folder = (Path(root) if root else cache_root()) / "downloads"
    folder.mkdir(parents=True, exist_ok=True)
    stem = hashlib.sha1(url.encode()).hexdigest()[:16]
    existing = finished_downloads(folder, stem)
    if existing:
        os.utime(existing[0])
        return existing[0]
    if shutil.which("yt-dlp") is None:
        raise VlError(EXIT_DEPENDENCY, "yt-dlp not found (needed for URL input)", "Install yt-dlp or pass a local file")
    result = subprocess.run(["yt-dlp", "--no-playlist", "--quiet", "--no-warnings", "--no-progress",
                             "-f", "bv*[height<=1080]+ba/b[height<=1080]/b", "--merge-output-format", "mp4",
                             "-o", str(folder / f"{stem}.%(ext)s"), url], capture_output=True, text=True)
    downloaded = finished_downloads(folder, stem)
    if result.returncode != 0 or not downloaded:
        reason = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else f"exit {result.returncode}"
        raise VlError(EXIT_INPUT, f"download failed: {reason}", "Check the URL, or download the video and pass the file")
    return downloaded[0]


def finished_downloads(folder, stem):
    return [f for f in sorted(folder.glob(f"{stem}.*")) if f.suffix not in PARTIAL_DOWNLOAD_SUFFIXES]
