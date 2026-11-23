from pathlib import Path

import pytest

from lambda_deps_builder.builder_handler import slimmer


def _write_bytes(path: Path, size: int, content_byte: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content_byte * size)
    return path


def test_slim_directory_prunes_bloat_and_preserves_runtime_and_testing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(slimmer.shutil, "which", lambda _: None)

    target = tmp_path / "python"
    # Runtime files (must be kept)
    runtime_py = _write_bytes(target / "pkg" / "__init__.py", 100)
    runtime_json = _write_bytes(target / "pkg" / "data.json", 80)
    runtime_so = _write_bytes(target / "pkg" / "_native.so", 500)
    testing_mod = _write_bytes(target / "numpy" / "testing" / "__init__.py", 120)

    # Bloat to prune
    _write_bytes(target / "pkg" / "__pycache__" / "__init__.cpython-312.pyc", 200)
    _write_bytes(target / "pkg" / "orphan.pyc", 50)
    _write_bytes(target / "pkg" / "legacy.pyo", 40)
    _write_bytes(target / "pkg" / "tests" / "test_core.py", 300)
    _write_bytes(target / "pkg" / "test" / "helpers.py", 150)
    _write_bytes(target / "pkg" / "docs" / "index.html", 250)
    _write_bytes(target / "pkg" / "stubs.pyi", 60)
    _write_bytes(target / "pkg" / "ext.c", 70)
    _write_bytes(target / "pkg" / "ext.h", 30)
    _write_bytes(target / "pkg" / "README.md", 90)
    _write_bytes(target / "pkg" / "LICENSE", 110)

    stats = slimmer.slim_directory(target)

    assert runtime_py.is_file()
    assert runtime_json.is_file()
    assert runtime_so.is_file()
    assert testing_mod.is_file()

    assert not (target / "pkg" / "__pycache__").exists()
    assert not (target / "pkg" / "orphan.pyc").exists()
    assert not (target / "pkg" / "legacy.pyo").exists()
    assert not (target / "pkg" / "tests").exists()
    assert not (target / "pkg" / "test").exists()
    assert not (target / "pkg" / "docs").exists()
    assert not (target / "pkg" / "stubs.pyi").exists()
    assert not (target / "pkg" / "ext.c").exists()
    assert not (target / "pkg" / "ext.h").exists()
    assert not (target / "pkg" / "README.md").exists()
    assert not (target / "pkg" / "LICENSE").exists()

    assert stats["enabled"] is True
    assert stats["uncompressed_after_bytes"] == 100 + 80 + 500 + 120
    assert stats["saved_bytes"] == 200 + 50 + 40 + 300 + 150 + 250 + 60 + 70 + 30 + 90 + 110
    assert stats["breakdown_bytes"]["bytecode"] == 200 + 50 + 40
    assert stats["breakdown_bytes"]["tests"] == 300 + 150
    assert stats["breakdown_bytes"]["docs_and_meta"] == 250 + 60 + 70 + 30 + 90 + 110


def test_slim_directory_dist_info_preservation_and_opt_in_pruning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(slimmer.shutil, "which", lambda _: None)

    target = tmp_path / "python"
    _write_bytes(target / "pkg" / "__init__.py", 50)
    meta = _write_bytes(target / "pkg-1.0.dist-info" / "METADATA", 100)
    ep = _write_bytes(target / "pkg-1.0.dist-info" / "entry_points.txt", 40)
    lic = _write_bytes(target / "pkg-1.0.dist-info" / "LICENSE", 60)

    # Default: prune_dist_info=False preserves dist-info and its files
    slimmer.slim_directory(target, prune_dist_info=False)
    assert meta.is_file()
    assert ep.is_file()
    assert lic.is_file()

    # Opt-in: prune_dist_info=True removes *.dist-info
    stats = slimmer.slim_directory(target, prune_dist_info=True)
    assert not (target / "pkg-1.0.dist-info").exists()
    assert stats["breakdown_bytes"]["docs_and_meta"] == 200


def test_slim_directory_prune_tests_false_keeps_tests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(slimmer.shutil, "which", lambda _: None)

    target = tmp_path / "python"
    test_file = _write_bytes(target / "pkg" / "tests" / "test_one.py", 100)
    slimmer.slim_directory(target, prune_tests=False)
    assert test_file.is_file()


def test_slim_directory_exclude_patterns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(slimmer.shutil, "which", lambda _: None)

    target = tmp_path / "python"
    keep = _write_bytes(target / "pkg" / "keep.py", 50)
    csv_file = _write_bytes(target / "pkg" / "sample.csv", 120)
    boto_file = _write_bytes(target / "boto3" / "__init__.py", 300)

    stats = slimmer.slim_directory(
        target, exclude_patterns=["boto3*", "*.csv"]
    )
    assert keep.is_file()
    assert not csv_file.exists()
    assert not boto_file.exists()
    assert stats["breakdown_bytes"]["custom_excludes"] == 420


def test_slim_directory_strips_shared_libraries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_strip = tmp_path / "bin" / "strip"
    fake_strip.parent.mkdir(parents=True)
    fake_strip.write_text("#!/bin/sh\n")
    monkeypatch.setattr(slimmer.shutil, "which", lambda name: str(fake_strip) if name == "strip" else None)

    target = tmp_path / "python"
    so1 = _write_bytes(target / "pkg" / "_ext.so", 1000)
    so2 = _write_bytes(target / "pkg" / "libfoo.so.1.2", 800)

    strip_calls: list[list[str]] = []

    def fake_run(cmd, check, capture_output):
        strip_calls.append([str(c) for c in cmd])
        so_path = Path(cmd[-1])
        # Simulate symbol stripping shrinking binary by half
        so_path.write_bytes(b"s" * (so_path.stat().st_size // 2))

        class _Res:
            returncode = 0
            stderr = b""

        return _Res()

    monkeypatch.setattr(slimmer.subprocess, "run", fake_run)

    stats = slimmer.slim_directory(target, strip_binaries=True)

    assert len(strip_calls) == 2
    assert all(call[1] == "--strip-unneeded" for call in strip_calls)
    assert so1.stat().st_size == 500
    assert so2.stat().st_size == 400
    assert stats["strip_available"] is True
    assert stats["binaries_stripped"] == 2
    assert stats["breakdown_bytes"]["stripped_binaries"] == 900
    assert stats["saved_bytes"] == 900


def test_slim_directory_graceful_when_strip_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(slimmer.shutil, "which", lambda _: None)
    target = tmp_path / "python"
    so_file = _write_bytes(target / "pkg" / "_ext.so", 500)

    stats = slimmer.slim_directory(target, strip_binaries=True)

    assert so_file.stat().st_size == 500
    assert stats["strip_available"] is False
    assert stats["binaries_stripped"] == 0
    captured = capsys.readouterr()
    assert "strip binary not found" in captured.out


def test_resolve_strip_binary_prefers_bundled_binary(tmp_path: Path) -> None:
    bundled = tmp_path / "bundled" / "strip"
    bundled.parent.mkdir(parents=True)
    bundled.write_text("#!/bin/sh\n")
    staged = tmp_path / "stage" / "strip"

    resolved = slimmer.resolve_strip_binary(bundled_strip=bundled, stage_strip=staged)
    assert resolved == staged
    assert staged.is_file()


def test_slim_directory_warns_when_exceeding_lambda_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(slimmer.shutil, "which", lambda _: None)
    monkeypatch.setattr(slimmer, "LAMBDA_UNZIPPED_LIMIT_BYTES", 10)

    target = tmp_path / "python"
    _write_bytes(target / "pkg" / "__init__.py", 100)

    slimmer.slim_directory(target)
    captured = capsys.readouterr()
    assert "exceeds AWS Lambda's 250 MB limit" in captured.out
