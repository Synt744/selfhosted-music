import os
import urllib.error
from pathlib import Path

import pytest

from cleaner.cleaner import Config, GIB, run_once, start_navidrome_scan


def make_config(
    cache_dir: Path,
    *,
    max_age_days: float = 30,
    max_size_gb: float = 20,
    min_age_minutes: float = 0,
    dry_run: bool = False,
) -> Config:
    return Config(
        cache_dir=cache_dir,
        max_age_days=max_age_days,
        max_size_gb=max_size_gb,
        min_age_minutes=min_age_minutes,
        interval_seconds=3600,
        dry_run=dry_run,
        navidrome_url="http://127.0.0.1:1",
        navidrome_user="",
        navidrome_password="",
        navidrome_timeout_seconds=0.1,
        lock_file=cache_dir.parent / "cleaner.lock",
    )


def create_file(path: Path, size: int, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    os.utime(path, (mtime, mtime))


def test_deletes_files_past_max_age(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    old = cache / "old.mp3"
    fresh = cache / "fresh.mp3"
    now = 2_000_000_000.0
    create_file(old, 10, now - 31 * 86400)
    create_file(fresh, 12, now - 2 * 86400)

    result = run_once(make_config(cache, max_age_days=30), now=now)

    assert result.removed_files == 1
    assert result.freed_bytes == 10
    assert not old.exists()
    assert fresh.exists()


def test_size_limit_removes_oldest_until_under_limit(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    now = 2_000_000_000.0
    create_file(cache / "old.mp3", 10, now - 3 * 86400)
    create_file(cache / "new.mp3", 12, now - 2 * 86400)

    result = run_once(
        make_config(cache, max_age_days=365, max_size_gb=15 / GIB),
        now=now,
    )

    assert result.removed_files == 1
    assert result.freed_bytes == 10
    assert not (cache / "old.mp3").exists()
    assert (cache / "new.mp3").exists()


def test_symlink_to_outside_cache_is_never_deleted(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    outside = tmp_path / "outside.mp3"
    cache.mkdir()
    create_file(outside, 25, 1_900_000_000.0)
    (cache / "escape.mp3").symlink_to(outside)

    result = run_once(
        make_config(cache, max_age_days=0, max_size_gb=1),
        now=2_000_000_000.0,
    )

    assert result.removed_files == 0
    assert outside.exists()
    assert (cache / "escape.mp3").is_symlink()


def test_minimum_age_protects_recent_file(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    now = 2_000_000_000.0
    recent = cache / "recent.mp3"
    create_file(recent, 15, now - 30 * 60)

    result = run_once(
        make_config(cache, max_age_days=0, max_size_gb=1, min_age_minutes=60),
        now=now,
    )

    assert result.removed_files == 0
    assert recent.exists()


def test_dry_run_reports_without_deleting(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    now = 2_000_000_000.0
    old = cache / "old.mp3"
    create_file(old, 20, now - 31 * 86400)

    result = run_once(make_config(cache, max_age_days=30, dry_run=True), now=now)

    assert result.dry_run is True
    assert result.selected_files == 1
    assert result.freed_bytes == 20
    assert result.removed_files == 0
    assert old.exists()


def test_removes_empty_subdirectories_after_deletion(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    now = 2_000_000_000.0
    nested = cache / "artist" / "album"
    old = nested / "old.mp3"
    create_file(old, 5, now - 31 * 86400)

    run_once(make_config(cache, max_age_days=30), now=now)

    assert not old.exists()
    assert not nested.exists()
    assert not (cache / "artist").exists()
    assert cache.is_dir()


def test_unavailable_navidrome_does_not_raise(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def unavailable(*_args: object, **_kwargs: object) -> None:
        raise urllib.error.URLError("offline")

    monkeypatch.setattr("urllib.request.urlopen", unavailable)
    config = make_config(tmp_path / "cache")
    config = Config(
        **{
            **config.__dict__,
            "navidrome_user": "cleaner",
            "navidrome_password": "not-a-real-secret",
        }
    )

    assert start_navidrome_scan(config) is False


def test_protected_extensions_are_not_deleted(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    now = 2_000_000_000.0
    partial = cache / "download.part"
    temporary = cache / "state.tmp"
    create_file(partial, 8, now - 100 * 86400)
    create_file(temporary, 9, now - 100 * 86400)

    result = run_once(
        make_config(cache, max_age_days=0, max_size_gb=1),
        now=now,
    )

    assert result.removed_files == 0
    assert partial.exists()
    assert temporary.exists()
