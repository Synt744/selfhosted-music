"""Safely expire old files from a mounted music cache."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
import os
import signal
import stat
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

GIB = 1024**3
PROTECTED_SUFFIXES = {".part", ".tmp"}
LOGGER = logging.getLogger("cleaner")
STOP_EVENT = threading.Event()


@dataclass(frozen=True)
class Config:
    cache_dir: Path
    max_age_days: float
    max_size_gb: float
    min_age_minutes: float
    interval_seconds: float
    dry_run: bool
    navidrome_url: str
    navidrome_user: str
    navidrome_password: str
    navidrome_timeout_seconds: float
    lock_file: Path


@dataclass(frozen=True)
class CacheFile:
    path: Path
    size: int
    mtime: float


@dataclass(frozen=True)
class CleanupResult:
    selected_files: int
    freed_bytes: int
    removed_files: int
    dry_run: bool


def _log(action: str, *, path: str = "-", size_bytes: int = 0, **details: object) -> None:
    record: dict[str, object] = {
        "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "action": action,
        "path": path,
        "size_bytes": size_bytes,
    }
    record.update(details)
    LOGGER.info("%s", json.dumps(record, ensure_ascii=False, sort_keys=True))


def _parse_bool(value: str, name: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _number(env: Mapping[str, str], name: str, default: str) -> float:
    try:
        value = float(env.get(name, default))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if value < 0:
        raise ValueError(f"{name} cannot be negative")
    return value


def config_from_env(env: Mapping[str, str] | None = None) -> Config:
    values = os.environ if env is None else env
    interval = _number(values, "CLEANER_INTERVAL_SECONDS", "3600")
    timeout = _number(values, "NAVIDROME_TIMEOUT_SECONDS", "8")
    if interval <= 0:
        raise ValueError("CLEANER_INTERVAL_SECONDS must be greater than zero")
    if timeout <= 0:
        raise ValueError("NAVIDROME_TIMEOUT_SECONDS must be greater than zero")
    max_size_gb = _number(values, "CACHE_MAX_SIZE_GB", "20")
    if max_size_gb <= 0:
        raise ValueError("CACHE_MAX_SIZE_GB must be greater than zero")

    return Config(
        cache_dir=Path(values.get("CACHE_DIR", "/cache")),
        max_age_days=_number(values, "CACHE_MAX_AGE_DAYS", "30"),
        max_size_gb=max_size_gb,
        min_age_minutes=_number(values, "CACHE_MIN_AGE_MINUTES", "60"),
        interval_seconds=interval,
        dry_run=_parse_bool(values.get("DRY_RUN", "true"), "DRY_RUN"),
        navidrome_url=values.get("NAVIDROME_URL", "http://navidrome:4533").rstrip("/"),
        navidrome_user=values.get("NAVIDROME_USER", ""),
        navidrome_password=values.get("NAVIDROME_PASSWORD", ""),
        navidrome_timeout_seconds=timeout,
        lock_file=Path(values.get("LOCK_FILE", "/tmp/cleaner.lock")),
    )


def _within_root(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((str(path), str(root))) == str(root)
    except ValueError:
        return False


def _scan_files(root: Path, now: float, min_age_seconds: float) -> tuple[int, list[CacheFile]]:
    total_bytes = 0
    eligible: list[CacheFile] = []

    for directory, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current_dir = Path(directory)
        safe_dirs: list[str] = []
        for dirname in dirnames:
            child_dir = current_dir / dirname
            try:
                if child_dir.is_symlink():
                    _log("SKIP_SYMLINK", path=str(child_dir))
                    continue
                real_dir = Path(os.path.realpath(child_dir))
                if _within_root(real_dir, root):
                    safe_dirs.append(dirname)
                else:
                    _log("SKIP_OUTSIDE_ROOT", path=str(child_dir))
            except OSError:
                _log("SKIP_UNREADABLE", path=str(child_dir))
        dirnames[:] = safe_dirs

        for filename in filenames:
            path = current_dir / filename
            try:
                info = path.lstat()
            except OSError:
                _log("SKIP_UNREADABLE", path=str(path))
                continue
            if stat.S_ISLNK(info.st_mode):
                _log("SKIP_SYMLINK", path=str(path))
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            real_path = Path(os.path.realpath(path))
            if not _within_root(real_path, root):
                _log("SKIP_OUTSIDE_ROOT", path=str(path))
                continue

            total_bytes += info.st_size
            age_seconds = now - info.st_mtime
            if age_seconds < min_age_seconds or path.suffix.lower() in PROTECTED_SUFFIXES:
                continue
            eligible.append(CacheFile(path=path, size=info.st_size, mtime=info.st_mtime))

    return total_bytes, eligible


def _choose_deletions(
    files: list[CacheFile],
    total_bytes: int,
    now: float,
    max_age_days: float,
    max_size_gb: float,
) -> tuple[list[CacheFile], int]:
    age_limit_seconds = max_age_days * 24 * 60 * 60
    selected: dict[Path, CacheFile] = {
        item.path: item for item in files if now - item.mtime > age_limit_seconds
    }
    remaining_bytes = total_bytes - sum(item.size for item in selected.values())
    size_limit_bytes = int(max_size_gb * GIB)

    if remaining_bytes > size_limit_bytes:
        for item in sorted(files, key=lambda entry: (entry.mtime, str(entry.path))):
            if item.path in selected:
                continue
            if remaining_bytes <= size_limit_bytes:
                break
            selected[item.path] = item
            remaining_bytes -= item.size

    ordered = sorted(selected.values(), key=lambda entry: (entry.mtime, str(entry.path)))
    return ordered, max(0, total_bytes - remaining_bytes)


def _is_safe_regular_file(path: Path, root: Path, now: float, min_age_seconds: float) -> os.stat_result | None:
    try:
        info = path.lstat()
        real_path = Path(os.path.realpath(path))
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return None
    if not _within_root(real_path, root) or now - info.st_mtime < min_age_seconds:
        return None
    return info


def _remove_empty_directories(root: Path, dry_run: bool) -> None:
    for directory, dirnames, _ in os.walk(root, topdown=False, followlinks=False):
        current = Path(directory)
        if current == root:
            continue
        try:
            real_path = Path(os.path.realpath(current))
            if not _within_root(real_path, root) or current.is_symlink():
                continue
            if dry_run:
                continue
            current.rmdir()
            _log("REMOVE_EMPTY_DIR", path=str(current))
        except OSError:
            # Non-empty directories, concurrent writes, and permission errors are harmless.
            continue


def start_navidrome_scan(config: Config) -> bool:
    if not config.navidrome_user or not config.navidrome_password:
        _log("SCAN_SKIPPED", reason="credentials_not_configured")
        return False

    salt = os.urandom(8).hex()
    token = hashlib.md5(
        (config.navidrome_password + salt).encode("utf-8"), usedforsecurity=False
    ).hexdigest()
    query = urllib.parse.urlencode(
        {
            "u": config.navidrome_user,
            "t": token,
            "s": salt,
            "v": "1.16.1",
            "c": "selfhosted-music-cleaner",
            "f": "json",
        }
    )
    url = f"{config.navidrome_url}/rest/startScan.view?{query}"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})

    try:
        with urllib.request.urlopen(request, timeout=config.navidrome_timeout_seconds) as response:
            result = json.loads(response.read().decode("utf-8"))
        status = result.get("subsonic-response", {}).get("status")
        if status != "ok":
            _log("SCAN_FAILED", reason="api_rejected_request")
            return False
        _log("SCAN_STARTED")
        return True
    except urllib.error.HTTPError as exc:
        _log("SCAN_FAILED", reason="http_error", status=exc.code)
    except (urllib.error.URLError, TimeoutError, OSError):
        _log("SCAN_FAILED", reason="navidrome_unavailable")
    except (json.JSONDecodeError, UnicodeDecodeError, AttributeError, TypeError):
        _log("SCAN_FAILED", reason="invalid_api_response")
    return False


def run_once(config: Config, now: float | None = None) -> CleanupResult:
    clock = time.time() if now is None else now
    root = Path(os.path.realpath(config.cache_dir))
    if not root.is_dir():
        raise NotADirectoryError(f"CACHE_DIR is not an accessible directory: {config.cache_dir}")

    min_age_seconds = config.min_age_minutes * 60
    total_bytes, eligible = _scan_files(root, clock, min_age_seconds)
    selected, planned_freed_bytes = _choose_deletions(
        eligible, total_bytes, clock, config.max_age_days, config.max_size_gb
    )
    for item in selected:
        _log(
            "DRY_RUN_DELETE" if config.dry_run else "DELETE",
            path=str(item.path),
            size_bytes=item.size,
        )

    removed_files = 0
    actual_freed_bytes = 0
    if not config.dry_run:
        for item in selected:
            info = _is_safe_regular_file(item.path, root, clock, min_age_seconds)
            if info is None or item.path.suffix.lower() in PROTECTED_SUFFIXES:
                _log("SKIP_CHANGED_OR_UNSAFE", path=str(item.path), size_bytes=item.size)
                continue
            try:
                os.unlink(item.path)
            except OSError:
                _log("DELETE_FAILED", path=str(item.path), size_bytes=info.st_size)
                continue
            removed_files += 1
            actual_freed_bytes += info.st_size
        _remove_empty_directories(root, dry_run=False)
        if removed_files:
            start_navidrome_scan(config)
    else:
        _remove_empty_directories(root, dry_run=True)

    freed = planned_freed_bytes if config.dry_run else actual_freed_bytes
    _log(
        "SUMMARY",
        files=removed_files if not config.dry_run else len(selected),
        freed_bytes=freed,
        freed_gib=round(freed / GIB, 6),
        dry_run=config.dry_run,
    )
    remaining_estimate = total_bytes - freed
    limit_bytes = int(config.max_size_gb * GIB)
    if remaining_estimate > limit_bytes:
        _log(
            "SIZE_LIMIT_UNMET",
            size_bytes=remaining_estimate,
            limit_bytes=limit_bytes,
            reason="protected_or_ineligible_files",
        )
    return CleanupResult(
        selected_files=len(selected),
        freed_bytes=freed,
        removed_files=removed_files,
        dry_run=config.dry_run,
    )


def _acquire_lock(path: Path) -> int | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        return None
    return descriptor


def _handle_signal(signum: int, _frame: object) -> None:
    _log("STOP_REQUESTED", signal=signum)
    STOP_EVENT.set()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="run one cleanup pass and exit")
    args = parser.parse_args(argv)

    try:
        config = config_from_env()
    except ValueError as exc:
        _log("CONFIG_ERROR", reason=str(exc))
        return 2

    try:
        lock_fd = _acquire_lock(config.lock_file)
    except OSError:
        _log("LOCK_FAILED", path=str(config.lock_file))
        return 2
    if lock_fd is None:
        _log("LOCK_BUSY", path=str(config.lock_file))
        return 0

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    try:
        while True:
            try:
                run_once(config)
            except OSError:
                _log("CLEANUP_FAILED", reason="cache_unavailable")
                if args.once:
                    return 1
            if args.once or STOP_EVENT.wait(config.interval_seconds):
                break
    finally:
        os.close(lock_fd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
