from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

LICENSE_URLS = {
    "CC0-1.0": "https://creativecommons.org/publicdomain/zero/1.0/",
    "CC-BY-4.0": "https://creativecommons.org/licenses/by/4.0/",
    "CC-BY-SA-4.0": "https://creativecommons.org/licenses/by-sa/4.0/",
    "CC-BY-NC-4.0": "https://creativecommons.org/licenses/by-nc/4.0/",
    "CC-BY-NC-SA-4.0": "https://creativecommons.org/licenses/by-nc-sa/4.0/",
    "PDM-1.0": "https://creativecommons.org/publicdomain/mark/1.0/",
}
AUDIO_SUFFIXES = frozenset(
    {".aif", ".aiff", ".alac", ".ape", ".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".wv"}
)
HOST_PATTERN = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
)


class ImportManifestError(ValueError):
    """Raised when a manifest, source list, or path fails validation."""


@dataclass(frozen=True)
class Track:
    source: Path
    destination: Path
    sidecar: Path
    license_id: str
    license_url: str
    source_url: str
    attribution: str
    artist: str
    album: str
    title: str


def _existing_root(value: Path | str, label: str) -> Path:
    path = Path(value)
    if path.is_symlink():
        raise ImportManifestError(f"{label} root must not be a symlink")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ImportManifestError(f"{label} root does not exist: {path}") from exc
    if not resolved.is_dir():
        raise ImportManifestError(f"{label} root is not a directory: {path}")
    return resolved


def _safe_file(root: Path, value: str, label: str, *, allow_absolute: bool) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ImportManifestError(f"{label} must be a non-empty path")
    given = Path(value)
    if given.is_absolute():
        if not allow_absolute:
            raise ImportManifestError(f"{label} must be relative to incoming")
        try:
            relative = given.relative_to(root)
        except ValueError as exc:
            raise ImportManifestError(f"{label} must stay under incoming") from exc
    else:
        relative = given
    if ".." in relative.parts or not relative.parts:
        raise ImportManifestError(f"{label} contains an unsafe path")
    candidate = root
    for part in relative.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise ImportManifestError(f"{label} must not contain symlinks")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ImportManifestError(f"{label} does not exist: {value}") from exc
    if not resolved.is_relative_to(root):
        raise ImportManifestError(f"{label} must stay under incoming")
    if not resolved.is_file():
        raise ImportManifestError(f"{label} is not a regular file")
    return resolved


def _https_host(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ImportManifestError(f"{label} must be an HTTPS URL")
    parsed = urlparse(value)
    host = parsed.hostname
    try:
        has_port = parsed.port is not None
    except ValueError as exc:
        raise ImportManifestError(f"{label} has an invalid port") from exc
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or has_port
        or parsed.netloc.lower() != host.lower()
    ):
        raise ImportManifestError(
            f"{label} must be a public HTTPS page URL without credentials or query parameters"
        )
    return host.lower()


def _load_allowed_hosts(sources_path: Path, incoming_root: Path) -> set[str]:
    source_file = _safe_file(
        incoming_root, str(sources_path), "sources file", allow_absolute=True
    )
    try:
        payload = json.loads(source_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ImportManifestError("Could not read sources.json") from exc
    hosts = payload.get("allowed_hosts") if isinstance(payload, dict) else None
    if not isinstance(hosts, list):
        raise ImportManifestError("sources.json must contain an allowed_hosts list")
    allowed: set[str] = set()
    for host in hosts:
        if not isinstance(host, str) or host != host.lower() or not HOST_PATTERN.fullmatch(host):
            raise ImportManifestError(
                "allowed_hosts must contain exact lowercase DNS hostnames, without wildcards"
            )
        allowed.add(host)
    if not allowed:
        raise ImportManifestError(
            "No source hosts are allowed; the owner must review and add exact hosts first"
        )
    return allowed


def _safe_component(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ImportManifestError(f"{label} must be a non-empty string")
    cleaned = re.sub(r"[^\w .()-]+", "_", value.strip(), flags=re.UNICODE)
    cleaned = cleaned.strip(" .")[:120].rstrip(" .")
    if not cleaned or cleaned in {".", ".."}:
        raise ImportManifestError(f"{label} does not make a safe path component")
    return cleaned


def _check_destination_parent(library_root: Path, destination: Path) -> None:
    try:
        relative = destination.relative_to(library_root)
    except ValueError as exc:
        raise ImportManifestError("destination escaped permanent") from exc
    current = library_root
    for part in relative.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise ImportManifestError("destination path must not contain symlinks")
        if current.exists() and not current.is_dir():
            raise ImportManifestError("destination parent exists and is not a directory")


def prepare_tracks(
    manifest_path: Path | str,
    sources_path: Path | str,
    incoming_root: Path | str,
    library_root: Path | str,
) -> list[Track]:
    incoming = _existing_root(incoming_root, "incoming")
    library = _existing_root(library_root, "permanent")
    allowed_hosts = _load_allowed_hosts(Path(sources_path), incoming)
    manifest_file = _safe_file(
        incoming, str(manifest_path), "manifest file", allow_absolute=True
    )
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ImportManifestError("Could not read manifest.json") from exc
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        raise ImportManifestError("manifest version must be 1")
    rows = manifest.get("tracks")
    if not isinstance(rows, list) or not rows:
        raise ImportManifestError("manifest tracks must be a non-empty list")

    tracks: list[Track] = []
    destinations: set[Path] = set()
    for index, row in enumerate(rows, start=1):
        label = f"tracks[{index}]"
        if not isinstance(row, dict):
            raise ImportManifestError(f"{label} must be an object")
        if row.get("rights_confirmed") is not True:
            raise ImportManifestError(f"{label}: rights_confirmed must be true")
        license_id = row.get("license_id")
        if not isinstance(license_id, str) or license_id not in LICENSE_URLS:
            raise ImportManifestError(f"{label}: unsupported or unknown license_id")
        license_url = row.get("license_url")
        _https_host(license_url, f"{label}.license_url")
        if license_url != LICENSE_URLS[license_id]:
            raise ImportManifestError(f"{label}: license_url does not match license_id")
        source_url = row.get("source_url")
        source_host = _https_host(source_url, f"{label}.source_url")
        if source_host not in allowed_hosts:
            raise ImportManifestError(f"{label}: source host is not allowlisted")
        attribution = row.get("attribution", "")
        if not isinstance(attribution, str):
            raise ImportManifestError(f"{label}.attribution must be a string")
        if license_id != "CC0-1.0" and not attribution.strip():
            raise ImportManifestError(f"{label}: attribution is required for this license")

        source = _safe_file(
            incoming, row.get("source_path"), f"{label}.source_path", allow_absolute=False
        )
        if source.suffix.lower() not in AUDIO_SUFFIXES:
            raise ImportManifestError(f"{label}: unsupported audio file extension")
        artist = _safe_component(row.get("artist"), f"{label}.artist")
        album = _safe_component(row.get("album"), f"{label}.album")
        title = _safe_component(row.get("title"), f"{label}.title")
        track_number = row.get("track_number")
        if track_number is not None:
            if isinstance(track_number, bool) or not isinstance(track_number, int):
                raise ImportManifestError(f"{label}.track_number must be a positive integer")
            if track_number < 1 or track_number > 9999:
                raise ImportManifestError(f"{label}.track_number must be a positive integer")
            filename = f"{track_number:02d} - {title}{source.suffix.lower()}"
        else:
            filename = f"{title}{source.suffix.lower()}"
        destination = library / artist / album / filename
        sidecar = destination.with_name(destination.name + ".license.json")
        _check_destination_parent(library, destination)
        if destination.exists() or destination.is_symlink():
            raise ImportManifestError(f"{label}: destination already exists; refusing overwrite")
        if sidecar.exists() or sidecar.is_symlink():
            raise ImportManifestError(f"{label}: license sidecar already exists; refusing overwrite")
        if destination in destinations:
            raise ImportManifestError(f"{label}: duplicate destination in manifest")
        destinations.add(destination)
        tracks.append(
            Track(
                source=source,
                destination=destination,
                sidecar=sidecar,
                license_id=license_id,
                license_url=license_url,
                source_url=source_url,
                attribution=attribution.strip(),
                artist=artist,
                album=album,
                title=title,
            )
        )
    return tracks


def _make_directories(library_root: Path, destination: Path) -> None:
    relative = destination.parent.relative_to(library_root)
    current = library_root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ImportManifestError("destination path changed to a symlink")
        if current.exists():
            if not current.is_dir():
                raise ImportManifestError("destination parent is not a directory")
        else:
            current.mkdir()


def _write_temp_file(parent: Path, prefix: str, writer: Any) -> Path:
    descriptor, name = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=parent)
    path = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            writer(handle)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def _copy_track(track: Track, library_root: Path) -> None:
    _make_directories(library_root, track.destination)
    if track.destination.exists() or track.destination.is_symlink():
        raise ImportManifestError("destination appeared during import; refusing overwrite")
    if track.sidecar.exists() or track.sidecar.is_symlink():
        raise ImportManifestError("license sidecar appeared during import; refusing overwrite")

    source_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    source_fd = os.open(track.source, source_flags)
    try:
        if not stat.S_ISREG(os.fstat(source_fd).st_mode):
            raise ImportManifestError("source is no longer a regular file")
        with os.fdopen(source_fd, "rb", closefd=False) as source_handle:
            audio_temp = _write_temp_file(
                track.destination.parent,
                ".ingest-audio-",
                lambda target: shutil.copyfileobj(source_handle, target),
            )
    finally:
        os.close(source_fd)

    record = {
        "artist": track.artist,
        "album": track.album,
        "title": track.title,
        "source_url": track.source_url,
        "license_id": track.license_id,
        "license_url": track.license_url,
        "attribution": track.attribution,
        "rights_confirmed_by_owner": True,
        "imported_at_utc": datetime.now(timezone.utc).isoformat(),
    }

    def write_record(handle: Any) -> None:
        handle.write(json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8"))
        handle.write(b"\n")

    try:
        metadata_temp = _write_temp_file(
            track.destination.parent, ".ingest-license-", write_record
        )
    except Exception:
        audio_temp.unlink(missing_ok=True)
        raise
    audio_linked = False
    try:
        os.link(audio_temp, track.destination)
        audio_linked = True
        os.link(metadata_temp, track.sidecar)
    except OSError as exc:
        if audio_linked:
            track.destination.unlink(missing_ok=True)
        raise ImportManifestError(
            "Could not commit files without overwriting an existing path"
        ) from exc
    finally:
        audio_temp.unlink(missing_ok=True)
        metadata_temp.unlink(missing_ok=True)


def _check_copy_space(tracks: list[Track], library_root: Path) -> None:
    required = sum(track.source.stat().st_size + 4096 for track in tracks)
    available = shutil.disk_usage(library_root).free
    if available < required:
        raise ImportManifestError(
            f"Not enough free disk space for this import: need about {required} bytes, have {available}"
        )


def run_import(
    manifest_path: Path | str,
    sources_path: Path | str,
    incoming_root: Path | str,
    library_root: Path | str,
    *,
    apply: bool = False,
) -> list[Track]:
    incoming = _existing_root(incoming_root, "incoming")
    library = _existing_root(library_root, "permanent")
    tracks = prepare_tracks(manifest_path, sources_path, incoming, library)
    if apply:
        _check_copy_space(tracks, library)
        for track in tracks:
            _copy_track(track, library)
    return tracks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate and import owner-authorized local audio files; no downloads."
    )
    parser.add_argument("--manifest", default="/incoming/manifest.json")
    parser.add_argument("--sources", default="/incoming/sources.json")
    parser.add_argument("--incoming", default="/incoming")
    parser.add_argument("--library", default="/permanent")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="copy validated files; default is dry-run with no writes",
    )
    args = parser.parse_args(argv)
    try:
        tracks = run_import(
            args.manifest,
            args.sources,
            args.incoming,
            args.library,
            apply=args.apply,
        )
    except (ImportManifestError, OSError) as exc:
        print(f"Import stopped: {exc}", file=sys.stderr)
        return 1
    mode = "IMPORTED" if args.apply else "DRY RUN"
    for track in tracks:
        print(f"{mode}: {track.source} -> {track.destination} [{track.license_id}]")
    if not args.apply:
        total = sum(track.source.stat().st_size for track in tracks)
        available = shutil.disk_usage(Path(args.library)).free
        print(f"Planned audio bytes: {total}; currently free in permanent: {available}")
        print("No files written. Review every source, license, and destination first.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
