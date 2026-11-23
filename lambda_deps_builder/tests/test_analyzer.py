"""Unit tests for `lambda_deps_builder.builder_handler.analyzer`."""

import json
import re
from pathlib import Path

from lambda_deps_builder.builder_handler import analyzer


def _make_dist_info(
    root: Path,
    dist_name: str,
    version: str,
    files: dict[str, bytes],
    *,
    dir_slug: str | None = None,
) -> None:
    """Create a `.dist-info` directory with `METADATA`, `RECORD`, and package files."""
    norm = (dir_slug or dist_name).replace("-", "_")
    dist_info = root / f"{norm}-{version}.dist-info"
    dist_info.mkdir(parents=True, exist_ok=True)
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {dist_name}\nVersion: {version}\n\nSummary: test\n",
        encoding="utf-8",
    )

    record_lines: list[str] = []
    for rel_path, content in files.items():
        target = root / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        record_lines.append(f"{rel_path},sha256=abc,{len(content)}")

    record_lines.append(f"{dist_info.name}/METADATA,sha256=abc,10")
    record_lines.append(f"{dist_info.name}/RECORD,,")
    (dist_info / "RECORD").write_text("\n".join(record_lines) + "\n", encoding="utf-8")


def test_analyze_directory_dist_info_attribution_and_native_bytes(tmp_path: Path) -> None:
    _make_dist_info(
        tmp_path,
        "requests",
        "2.32.3",
        {
            "requests/__init__.py": b"x" * 100,
            "requests/api.py": b"y" * 200,
        },
    )
    _make_dist_info(
        tmp_path,
        "numpy",
        "2.1.0",
        {
            "numpy/__init__.py": b"n" * 50,
            "numpy/_core/_multiarray.cpython-312-x86_64-linux-gnu.so": b"s" * 1000,
            "numpy.libs/libopenblas.so.0": b"l" * 500,
        },
    )

    result = analyzer.analyze_directory(tmp_path)

    assert result["package_count"] == 2
    assert result["quota_limit_bytes"] == 262_144_000
    assert result["native_binary_bytes"] == 1500
    assert result["total_unzipped_bytes"] > 1850

    pkgs = {p["name"]: p for p in result["packages"]}
    assert result["packages"][0]["name"] == "numpy"
    assert pkgs["numpy"]["version"] == "2.1.0"
    assert pkgs["numpy"]["native_bytes"] == 1500
    assert pkgs["requests"]["version"] == "2.32.3"
    assert pkgs["requests"]["native_bytes"] == 0

    # Verify nested tree structure exists under numpy
    numpy_children = {c["name"]: c for c in pkgs["numpy"]["children"]}
    assert "numpy" in numpy_children
    assert "numpy.libs" in numpy_children


def test_analyze_directory_fallback_for_unrecorded_files(tmp_path: Path) -> None:
    custom_dir = tmp_path / "custom_mod"
    custom_dir.mkdir()
    (custom_dir / "helper.py").write_bytes(b"a" * 300)
    (tmp_path / "top_script.py").write_bytes(b"b" * 120)

    result = analyzer.analyze_directory(tmp_path)

    assert result["package_count"] == 2
    assert result["total_unzipped_bytes"] == 420
    assert result["file_count"] == 2
    pkgs = {p["name"]: p for p in result["packages"]}
    assert pkgs["custom_mod"]["size"] == 300
    assert pkgs["custom_mod"]["version"] == ""
    assert pkgs["(root)"]["size"] == 120


def test_analyze_directory_ignores_escaping_record_paths(tmp_path: Path) -> None:
    dist_info = tmp_path / "evil-1.0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text("Name: evil\nVersion: 1.0\n", encoding="utf-8")
    (dist_info / "RECORD").write_text("../../etc/passwd,,\n", encoding="utf-8")

    result = analyzer.analyze_directory(tmp_path)
    pkgs = {p["name"]: p for p in result["packages"]}
    assert "evil" in pkgs
    # Only METADATA and RECORD inside dist-info are counted
    assert pkgs["evil"]["file_count"] == 2


def test_render_treemap_html_is_self_contained_and_escapes_xss(tmp_path: Path) -> None:
    _make_dist_info(
        tmp_path,
        "pkg</script><script>alert(1)</script>",
        "1.0.0",
        {"evil_mod/__init__.py": b"z" * 256},
        dir_slug="evil",
    )
    analysis = analyzer.analyze_directory(tmp_path)

    html_out = analyzer.render_treemap_html(
        analysis,
        zip_bytes=128,
        metadata={
            "runtime": "python3.12",
            "architecture": "x86_64",
            "engine": "uv",
            "build_duration_sec": 2.45,
            "object_key": "deps-x86_64-Deps.zip",
        },
    )

    assert html_out.startswith("<!DOCTYPE html>")
    assert not re.search(r'<script[^>]+src=["\']https?://', html_out, re.IGNORECASE)
    assert not re.search(r'<link[^>]+href=["\']https?://', html_out, re.IGNORECASE)
    assert "</script><script>alert(1)</script>" not in html_out

    match = re.search(
        r'<script id="layer-data" type="application/json">(.*?)</script>',
        html_out,
        re.DOTALL,
    )
    assert match is not None
    embedded = json.loads(match.group(1).replace("<\\/", "</"))
    assert embedded["zip_bytes"] == 128
    assert embedded["metadata"]["architecture"] == "x86_64"
    assert embedded["analysis"]["package_count"] == 1
