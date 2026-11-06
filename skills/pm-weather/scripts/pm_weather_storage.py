"""Weather storage policy. Import and path resolution never create directories.

Default cache is process memory; PM_WEATHER_CACHE_MODE=memory|disk|off selects it.
Disk persistence is opt-in. Record persistence is separately enabled by the CLI.
Overrides: PM_WEATHER_CACHE_DIR, PM_WEATHER_DATA_DIR (absolute paths),
PM_WEATHER_CACHE_TTL_HOURS (maximum age, default 168), and
PM_WEATHER_CACHE_MAX_MB (default 256). Zero disables cache retention.
Cleanup is flat, skips links, and examines at most 10000 entries per write.
Only recognized disposable weather cache names are eligible; ledgers/rules and
all .mimosa paths are protected even under overlapping directory overrides.
"""
from __future__ import annotations

import hashlib
import itertools
import math
import os
import pathlib
import re
import sys
import tempfile
import threading
import time

_LOCK = threading.RLock()
_CACHE_NAME = re.compile(r"(?:[0-9a-f]{32}|stats_[0-9a-f]{24}|(?:asos|prevparse|memparse)_[A-Za-z0-9_.-]+)\.(?:json|txt|csv)$")


def storage_paths(env=None, platform=None, home=None):
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    home = pathlib.Path.home() if home is None else pathlib.Path(home)
    if platform == "win32":
        base = pathlib.Path(env.get("LOCALAPPDATA") or home / "AppData" / "Local")
        cache_base = data_base = base
    elif platform == "darwin":
        cache_base, data_base = home / "Library/Caches", home / "Library/Application Support"
    else:
        cache_base = pathlib.Path(env.get("XDG_CACHE_HOME") or home / ".cache")
        data_base = pathlib.Path(env.get("XDG_DATA_HOME") or home / ".local/share")
    cache = pathlib.Path(env.get("PM_WEATHER_CACHE_DIR") or cache_base / "trading-skills/pm-weather/cache").expanduser()
    data = pathlib.Path(env.get("PM_WEATHER_DATA_DIR") or data_base / "trading-skills/pm-weather/data").expanduser()
    for path in (cache, data):
        if not path.is_absolute():
            raise ValueError("Weather storage paths must be absolute")
        if any(p.lower() == ".mimosa" for p in path.resolve().parts):
            raise ValueError("Weather storage cannot use a .mimosa subtree")
    return cache, data


def _number(name, default):
    value = float(os.environ.get(name, default))
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite and nonnegative")
    return value


def ttl_hours(requested):
    return min(requested, _number("PM_WEATHER_CACHE_TTL_HOURS", 168))


def fresh(path, hours):
    path = pathlib.Path(path)
    try:
        return not path.is_symlink() and path.is_file() and time.time() - path.stat().st_mtime < ttl_hours(hours) * 3600
    except OSError:
        return False


def _safe(path):
    return not any(p.lower() == ".mimosa" for p in pathlib.Path(path).resolve().parts)


def prune_cache(root, data_root):
    """Bounded best-effort oldest-first eviction, never a recursive deletion."""
    root, data_root = pathlib.Path(root), pathlib.Path(data_root).resolve()
    if not _safe(root) or not root.exists() or root.is_symlink():
        return
    age = ttl_hours(float("inf")) * 3600
    limit = _number("PM_WEATHER_CACHE_MAX_MB", 256) * 1024 * 1024
    with _LOCK:
        files = []
        with os.scandir(root) as entries:
            for entry in itertools.islice(entries, 10000):
                path = pathlib.Path(entry.path)
                if not _CACHE_NAME.fullmatch(entry.name) or entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                    continue
                resolved = path.resolve()
                if not _safe(path) or resolved == data_root or data_root in resolved.parents:
                    continue
                try:
                    stat = entry.stat(follow_symlinks=False)
                    files.append((stat.st_mtime, stat.st_size, path))
                except OSError:
                    pass
        total = sum(size for _, size, _ in files)
        now = time.time()
        for stamp, size, path in sorted(files):
            if now - stamp >= age or total > limit:
                try:
                    path.unlink()
                    total -= size
                except OSError:
                    pass


def atomic_text(path, text):
    """Create parents only on write, and never expose partially written JSON."""
    path = pathlib.Path(path)
    if not _safe(path):
        raise ValueError("Refusing to write into .mimosa")
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as fh:
            name = fh.name
            fh.write(text)
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


_MEMORY = {}


def cache_mode(value=None):
    value = os.environ.get("PM_WEATHER_CACHE_MODE", "memory") if value is None else value
    if value not in ("memory", "disk", "off"):
        raise ValueError("PM_WEATHER_CACHE_MODE must be memory, disk, or off")
    return value


def cache_fresh(path, hours, mode=None):
    mode = cache_mode(mode)
    if mode == "disk":
        return fresh(path, hours)
    if mode == "off":
        return False
    with _LOCK:
        item = _MEMORY.get(str(path))
        return item is not None and time.monotonic() - item[0] < ttl_hours(hours) * 3600


def read_cache(path, mode=None):
    mode = cache_mode(mode)
    if mode == "disk":
        return pathlib.Path(path).read_text(encoding="utf-8")
    if mode == "off":
        raise KeyError(path)
    with _LOCK:
        return _MEMORY[str(path)][1]


def write_cache(path, text, root, data_root, mode=None):
    mode = cache_mode(mode)
    with _LOCK:
        if mode == "disk":
            atomic_text(path, text)
            prune_cache(root, data_root)
        elif mode == "memory":
            _MEMORY[str(path)] = (time.monotonic(), text)


def store_rule(text, directory):
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    path = pathlib.Path(directory) / "rules" / (digest + ".txt")
    if not path.exists():
        atomic_text(path, text)
    return digest
