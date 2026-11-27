import hashlib
import os
from pathlib import Path
import subprocess

import pytest

from app.rag import bulk_import
from app.rag.storage import UnsafeFileError


def test_open_import_source_pins_and_hashes_the_regular_file(tmp_path: Path):
    root = tmp_path / "imports"
    root.mkdir()
    source_path = root / "report.txt"
    payload = b"synthetic imported content"
    source_path.write_bytes(payload)

    with bulk_import._open_import_source(root, source_path) as source:
        assert bulk_import._sha256(source) == hashlib.sha256(payload).hexdigest()
        assert source.read() == payload


def test_open_import_source_rejects_a_file_swapped_between_stat_and_open(tmp_path: Path, monkeypatch):
    root = tmp_path / "imports"
    root.mkdir()
    source_path = root / "report.txt"
    source_path.write_text("approved in-root data", encoding="utf-8")
    outside_path = tmp_path / "outside.txt"
    outside_path.write_text("outside data that must not be read", encoding="utf-8")
    real_open = os.open
    opened_descriptors: list[int] = []

    def swap_open(path, flags, *args, **kwargs):
        if Path(path) == source_path:
            descriptor = real_open(outside_path, flags, *args, **kwargs)
            opened_descriptors.append(descriptor)
            return descriptor
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(bulk_import.os, "open", swap_open)
    with pytest.raises(UnsafeFileError, match="changed or escaped"):
        bulk_import._open_import_source(root, source_path)

    assert len(opened_descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(opened_descriptors[0])
    assert outside_path.read_text(encoding="utf-8") == "outside data that must not be read"


def test_folder_walk_streams_a_large_synthetic_tree(tmp_path: Path):
    root = tmp_path / "imports"
    for directory in range(10):
        child = root / f"batch-{directory:02d}"
        child.mkdir(parents=True)
        for item in range(100):
            (child / f"report-{item:03d}.txt").write_bytes(b"x")
    (root / "ignored.bin").write_bytes(b"x")

    assert sum(1 for _ in bulk_import._files(root)) == 1000


@pytest.mark.skipif(os.name != "nt", reason="Windows directory junction behavior is platform-specific")
def test_folder_walk_and_open_reject_a_directory_junction_escape(tmp_path: Path):
    root = tmp_path / "imports"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "outside.txt"
    outside_file.write_text("outside synthetic content", encoding="utf-8")
    junction = root / "escaped-junction"

    created = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True, text=True, check=False,
    )
    if created.returncode != 0:
        detail = (created.stderr or created.stdout).strip()[-240:]
        pytest.skip(f"Windows junction creation was unavailable in this test process: {detail}")

    assert bulk_import._is_link_or_junction(junction)
    assert list(bulk_import._files(root)) == []
    with pytest.raises(UnsafeFileError, match="changed or escaped"):
        bulk_import._open_import_source(root, junction / outside_file.name)
    assert outside_file.read_text(encoding="utf-8") == "outside synthetic content"
