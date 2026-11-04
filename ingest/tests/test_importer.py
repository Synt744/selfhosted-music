import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ingest.importer import ImportManifestError, run_import

CC0_URL = "https://creativecommons.org/publicdomain/zero/1.0/"
CC_BY_URL = "https://creativecommons.org/licenses/by/4.0/"


def create_case(tmp_path: Path, *, license_id: str = "CC-BY-4.0") -> tuple[Path, Path, Path, Path]:
    incoming = tmp_path / "incoming"
    library = tmp_path / "permanent"
    incoming.mkdir()
    library.mkdir()
    source = incoming / "track.mp3"
    source.write_bytes(b"authorized audio fixture")
    license_url = CC0_URL if license_id == "CC0-1.0" else CC_BY_URL
    record = {
        "version": 1,
        "tracks": [
            {
                "source_path": "track.mp3",
                "artist": "Test Artist",
                "album": "Test Album",
                "title": "Test Song",
                "track_number": 1,
                "source_url": "https://example.org/release/1",
                "license_id": license_id,
                "license_url": license_url,
                "attribution": "Test Artist" if license_id != "CC0-1.0" else "",
                "rights_confirmed": True,
            }
        ],
    }
    manifest = incoming / "manifest.json"
    manifest.write_text(json.dumps(record), encoding="utf-8")
    sources = incoming / "sources.json"
    sources.write_text(json.dumps({"allowed_hosts": ["example.org"]}), encoding="utf-8")
    return incoming, library, manifest, sources


def test_default_dry_run_does_not_write_or_move_source(tmp_path: Path) -> None:
    incoming, library, manifest, sources = create_case(tmp_path)

    tracks = run_import(manifest, sources, incoming, library)

    assert len(tracks) == 1
    assert tracks[0].destination == library / "Test Artist" / "Test Album" / "01 - Test Song.mp3"
    assert tracks[0].source.read_bytes() == b"authorized audio fixture"
    assert not tracks[0].destination.exists()


def test_apply_copies_source_and_writes_license_sidecar(tmp_path: Path) -> None:
    incoming, library, manifest, sources = create_case(tmp_path)

    tracks = run_import(manifest, sources, incoming, library, apply=True)

    track = tracks[0]
    assert track.destination.read_bytes() == b"authorized audio fixture"
    assert track.source.read_bytes() == b"authorized audio fixture"
    metadata = json.loads(track.sidecar.read_text(encoding="utf-8"))
    assert metadata["license_id"] == "CC-BY-4.0"
    assert metadata["source_url"] == "https://example.org/release/1"
    assert metadata["rights_confirmed_by_owner"] is True


def test_existing_destination_is_never_overwritten(tmp_path: Path) -> None:
    incoming, library, manifest, sources = create_case(tmp_path)
    destination = library / "Test Artist" / "Test Album" / "01 - Test Song.mp3"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"keep existing")

    with pytest.raises(ImportManifestError, match="already exists"):
        run_import(manifest, sources, incoming, library, apply=True)

    assert destination.read_bytes() == b"keep existing"


def test_unallowlisted_source_host_is_rejected(tmp_path: Path) -> None:
    incoming, library, manifest, sources = create_case(tmp_path)
    record = json.loads(manifest.read_text(encoding="utf-8"))
    record["tracks"][0]["source_url"] = "https://other.example/release/1"
    manifest.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(ImportManifestError, match="not allowlisted"):
        run_import(manifest, sources, incoming, library)


def test_unknown_license_is_rejected(tmp_path: Path) -> None:
    incoming, library, manifest, sources = create_case(tmp_path)
    record = json.loads(manifest.read_text(encoding="utf-8"))
    record["tracks"][0]["license_id"] = "UNKNOWN"
    manifest.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(ImportManifestError, match="unknown license"):
        run_import(manifest, sources, incoming, library)


def test_license_url_must_match_license_id(tmp_path: Path) -> None:
    incoming, library, manifest, sources = create_case(tmp_path)
    record = json.loads(manifest.read_text(encoding="utf-8"))
    record["tracks"][0]["license_url"] = CC0_URL
    manifest.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(ImportManifestError, match="does not match"):
        run_import(manifest, sources, incoming, library)


def test_path_traversal_and_symlink_sources_are_rejected(tmp_path: Path) -> None:
    incoming, library, manifest, sources = create_case(tmp_path)
    record = json.loads(manifest.read_text(encoding="utf-8"))
    record["tracks"][0]["source_path"] = "../outside.mp3"
    manifest.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(ImportManifestError, match="unsafe path"):
        run_import(manifest, sources, incoming, library)

    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"outside")
    link = incoming / "linked.mp3"
    link.symlink_to(outside)
    record["tracks"][0]["source_path"] = "linked.mp3"
    manifest.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ImportManifestError, match="symlinks"):
        run_import(manifest, sources, incoming, library)


def test_empty_source_allowlist_blocks_import(tmp_path: Path) -> None:
    incoming, library, manifest, sources = create_case(tmp_path)
    sources.write_text(json.dumps({"allowed_hosts": []}), encoding="utf-8")

    with pytest.raises(ImportManifestError, match="No source hosts"):
        run_import(manifest, sources, incoming, library)


def test_apply_stops_before_writing_when_disk_is_too_small(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    incoming, library, manifest, sources = create_case(tmp_path)
    monkeypatch.setattr(
        "ingest.importer.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=0),
    )

    with pytest.raises(ImportManifestError, match="Not enough free disk space"):
        run_import(manifest, sources, incoming, library, apply=True)

    assert not (library / "Test Artist" / "Test Album").exists()
