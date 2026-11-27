import json
from pathlib import Path

import pytest

from app.ops import deployment_snapshot as snapshot_ops


@pytest.fixture
def snapshot_sources(tmp_path):
    app_data = tmp_path / "application-data"
    qdrant = tmp_path / "qdrant-data"
    app_data.mkdir()
    qdrant.mkdir()
    (app_data / "uploads").mkdir()
    (app_data / "uploads" / "synthetic.txt").write_text("synthetic source", encoding="utf-8")
    (app_data / "vector_index").mkdir()
    (app_data / "vector_index" / "index.faiss").write_bytes(b"synthetic vector index")
    (qdrant / "collection.snapshot").write_bytes(b"synthetic qdrant state")
    database_dump = tmp_path / "database.dump"
    database_dump.write_bytes(b"synthetic pg archive bytes")
    return app_data, qdrant, database_dump


@pytest.fixture
def valid_pg_archive(monkeypatch):
    monkeypatch.setattr(snapshot_ops, "verify_backup", lambda _path: 3)


def test_snapshot_copies_all_roots_and_verifies_hashes(tmp_path, snapshot_sources, valid_pg_archive):
    app_data, qdrant, database_dump = snapshot_sources
    target = tmp_path / "snapshot"
    result = snapshot_ops.create_deployment_snapshot(target, database_dump, app_data, qdrant)

    assert result["verified"] is True
    assert result["files"] == 4
    assert (target / "application-data" / "uploads" / "synthetic.txt").read_text(encoding="utf-8") == "synthetic source"
    assert (target / "application-data" / "vector_index" / "index.faiss").read_bytes() == b"synthetic vector index"
    assert (target / "qdrant-data" / "collection.snapshot").read_bytes() == b"synthetic qdrant state"
    assert snapshot_ops.verify_deployment_snapshot(target)["verified"] is True
    assert (app_data / "uploads" / "synthetic.txt").read_text(encoding="utf-8") == "synthetic source"
    assert database_dump.read_bytes() == b"synthetic pg archive bytes"


def test_snapshot_dereferences_internal_file_symlink_and_restores_as_regular_file(
    tmp_path, snapshot_sources, valid_pg_archive
):
    app_data, _qdrant, database_dump = snapshot_sources
    blobs = app_data / "models" / "blobs"
    blobs.mkdir(parents=True)
    source = blobs / "model.bin"
    source.write_bytes(b"synthetic cached model")
    link = app_data / "models" / "model-current.bin"
    try:
        link.symlink_to(Path("blobs") / "model.bin")
    except OSError as exc:
        pytest.skip(f"file symlinks are unavailable for this test account: {type(exc).__name__}")

    bundle = tmp_path / "snapshot-with-internal-link"
    result = snapshot_ops.create_deployment_snapshot(bundle, database_dump, app_data)
    assert result["verified"] is True
    copied_link = bundle / "application-data" / "models" / "model-current.bin"
    assert not copied_link.is_symlink()
    assert copied_link.read_bytes() == b"synthetic cached model"
    assert snapshot_ops.verify_deployment_snapshot(bundle)["verified"] is True

    restored = tmp_path / "restored-app-data"
    snapshot_ops.restore_snapshot_files(bundle, restored)
    restored_link = restored / "models" / "model-current.bin"
    assert not restored_link.is_symlink()
    assert restored_link.read_bytes() == b"synthetic cached model"
    assert link.is_symlink() and source.read_bytes() == b"synthetic cached model"


@pytest.mark.parametrize("kind", ["outside", "broken", "directory"])
def test_snapshot_rejects_unsafe_or_non_file_symlinks(tmp_path, snapshot_sources, valid_pg_archive, kind):
    app_data, _qdrant, database_dump = snapshot_sources
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("must not be copied", encoding="utf-8")
    link = app_data / "unsafe-link"
    try:
        if kind == "outside":
            link.symlink_to(outside)
        elif kind == "broken":
            link.symlink_to(app_data / "missing-target")
        else:
            target = app_data / "nested-directory"
            target.mkdir()
            link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable for this test account: {type(exc).__name__}")

    with pytest.raises(ValueError, match="symbolic link"):
        snapshot_ops.create_deployment_snapshot(tmp_path / "rejected-snapshot", database_dump, app_data)
    assert outside.read_text(encoding="utf-8") == "must not be copied"
    assert not (tmp_path / "rejected-snapshot" / "application-data" / "unsafe-link").exists()


def test_snapshot_verification_detects_tamper_and_unlisted_files(tmp_path, snapshot_sources, valid_pg_archive):
    app_data, _qdrant, database_dump = snapshot_sources
    target = tmp_path / "snapshot"
    snapshot_ops.create_deployment_snapshot(target, database_dump, app_data)
    (target / "application-data" / "uploads" / "synthetic.txt").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="integrity"):
        snapshot_ops.verify_deployment_snapshot(target)

    (target / "application-data" / "uploads" / "synthetic.txt").write_text("synthetic source", encoding="utf-8")
    (target / "unexpected.bin").write_bytes(b"unlisted")
    with pytest.raises(ValueError, match="inventory"):
        snapshot_ops.verify_deployment_snapshot(target)


def test_snapshot_refuses_existing_destination_without_overwriting(tmp_path, snapshot_sources, valid_pg_archive):
    app_data, _qdrant, database_dump = snapshot_sources
    target = tmp_path / "snapshot"
    target.mkdir()
    marker = target / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    with pytest.raises(FileExistsError):
        snapshot_ops.create_deployment_snapshot(target, database_dump, app_data)
    assert marker.read_text(encoding="utf-8") == "keep"


def test_restore_requires_new_targets_and_preserves_existing_data(tmp_path, snapshot_sources, valid_pg_archive):
    app_data, qdrant, database_dump = snapshot_sources
    bundle = tmp_path / "snapshot"
    snapshot_ops.create_deployment_snapshot(bundle, database_dump, app_data, qdrant)
    restored_app = tmp_path / "restored-app"
    restored_qdrant = tmp_path / "restored-qdrant"
    result = snapshot_ops.restore_snapshot_files(bundle, restored_app, restored_qdrant)
    assert Path(result["application-data"]) == restored_app
    assert (restored_app / "vector_index" / "index.faiss").read_bytes() == b"synthetic vector index"
    assert (restored_qdrant / "collection.snapshot").read_bytes() == b"synthetic qdrant state"

    marker = tmp_path / "existing"
    marker.mkdir()
    (marker / "keep.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(FileExistsError):
        snapshot_ops.restore_snapshot_files(bundle, marker, tmp_path / "new-qdrant")
    assert (marker / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert not (tmp_path / "new-qdrant").exists()


def test_manifest_path_traversal_is_rejected(tmp_path, snapshot_sources, valid_pg_archive):
    app_data, _qdrant, database_dump = snapshot_sources
    bundle = tmp_path / "snapshot"
    snapshot_ops.create_deployment_snapshot(bundle, database_dump, app_data)
    manifest_path = bundle / snapshot_ops.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    record = manifest["files"].pop("application-data/uploads/synthetic.txt")
    manifest["files"]["application-data/../outside.txt"] = record
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="unsafe path"):
        snapshot_ops.verify_deployment_snapshot(bundle)


def test_snapshot_rejects_source_inside_destination(tmp_path, valid_pg_archive):
    database_dump = tmp_path / "database.dump"
    database_dump.write_bytes(b"synthetic pg archive bytes")
    data = tmp_path / "application-data"
    nested = data / "snapshot"
    data.mkdir()
    with pytest.raises(ValueError, match="inside a source"):
        snapshot_ops.create_deployment_snapshot(nested, database_dump, data)
    assert not nested.exists()


def test_snapshot_rejects_unverified_database_before_creating_bundle(tmp_path, snapshot_sources, monkeypatch):
    app_data, _qdrant, database_dump = snapshot_sources
    monkeypatch.setattr(snapshot_ops, "verify_backup", lambda _path: 0)
    target = tmp_path / "snapshot"
    with pytest.raises(ValueError, match="no catalog entries"):
        snapshot_ops.create_deployment_snapshot(target, database_dump, app_data)
    assert not target.exists()

