import fnmatch
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

LAMBDA_UNZIPPED_LIMIT_BYTES = 262_144_000  # 250 MB

_BYTECODE_DIRS = {"__pycache__"}
_BYTECODE_SUFFIXES = {".pyc", ".pyo"}
# Deliberately exclude "testing" (e.g. numpy.testing is imported at runtime by pandas/scipy).
_TEST_DIRS = {"tests", "test"}
_DOC_DIRS = {"docs", "doc", "examples", "example"}
_COMPILE_AND_STUB_SUFFIXES = {
    ".pyi",
    ".c",
    ".cpp",
    ".cc",
    ".h",
    ".hpp",
    ".pxd",
    ".pyx",
    ".a",
    ".o",
}
_DOC_FILENAMES_PREFIXES = (
    "readme",
    "license",
    "copying",
    "notice",
    "authors",
    "changelog",
    "history",
    "contributing",
)
_DOC_SUFFIXES = {".md", ".rst"}
_DIST_INFO_SUFFIXES = (".dist-info", ".egg-info")


def calculate_dir_size(root: Path) -> int:
    """Return total byte size of all non-symlink regular files under ``root``."""
    total = 0
    for dirpath, _, filenames in os.walk(root, followlinks=False):
        for fname in filenames:
            fpath = Path(dirpath) / fname
            try:
                st = fpath.lstat()
            except OSError:
                continue
            if not fpath.is_symlink() and os.path.isfile(fpath):
                total += st.st_size
    return total


def resolve_strip_binary(
    bundled_strip: Optional[Path] = None,
    stage_strip: Optional[Path] = None,
) -> Optional[Path]:
    """
    Locate an ELF symbol stripping binary.

    Checks ``bundled_strip`` first (copying it to ``stage_strip`` with ``0o755``
    permissions so it is executable on Lambda), then falls back to ``PATH``
    lookups for ``strip``, ``eu-strip``, or ``llvm-strip``.
    """
    if bundled_strip is not None and bundled_strip.is_file():
        if stage_strip is not None:
            if not stage_strip.is_file():
                stage_strip.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(bundled_strip, stage_strip)
                try:
                    stage_strip.chmod(0o755)
                except OSError:
                    pass
            return stage_strip
        return bundled_strip

    for candidate in ("strip", "eu-strip", "llvm-strip"):
        found = shutil.which(candidate)
        if found is not None:
            return Path(found)
    return None


def _is_shared_library(path: Path) -> bool:
    """Return True if ``path`` is a regular ``.so`` or versioned ``.so.*`` file."""
    if path.is_symlink() or not path.is_file():
        return False
    return path.suffix == ".so" or ".so." in path.name


def strip_shared_libraries(target: Path, strip_bin: Path) -> tuple[int, int]:
    """
    Run ``strip --strip-unneeded`` on all ``.so`` / ``.so.*`` files under ``target``.

    Individual strip failures are logged and skipped so an unusual binary never
    breaks a deployment. Returns ``(binaries_stripped, bytes_saved)``.
    """
    binaries_stripped = 0
    bytes_saved = 0

    for dirpath, _, filenames in os.walk(target, followlinks=False):
        for fname in filenames:
            so_file = Path(dirpath) / fname
            if not _is_shared_library(so_file):
                continue
            try:
                before = so_file.stat().st_size
                res = subprocess.run(
                    [str(strip_bin), "--strip-unneeded", str(so_file)],
                    check=False,
                    capture_output=True,
                )
            except OSError as exc:
                print(f"[WARN] failed to run {strip_bin} on {so_file}: {exc}")
                continue

            if res.returncode == 0:
                try:
                    after = so_file.stat().st_size
                except OSError:
                    after = before
                bytes_saved += max(0, before - after)
                binaries_stripped += 1
            else:
                stderr = res.stderr.decode(errors="replace").strip() if res.stderr else ""
                print(
                    f"[WARN] strip exited {res.returncode} on {so_file.name}: {stderr}"
                )

    return binaries_stripped, bytes_saved


def _matches_any(rel_posix: str, name: str, patterns: list[str]) -> bool:
    """Match a relative POSIX path or basename against glob patterns."""
    for pat in patterns:
        if fnmatch.fnmatch(rel_posix, pat) or fnmatch.fnmatch(name, pat):
            return True
    return False


def _is_inside_dist_info(rel_parts: tuple[str, ...]) -> bool:
    return any(part.endswith(_DIST_INFO_SUFFIXES) for part in rel_parts)


def _fmt_mb(num_bytes: int) -> str:
    return f"{num_bytes / (1024 * 1024):.2f}"


def slim_directory(
    target: Path,
    *,
    strip_binaries: bool = True,
    prune_tests: bool = True,
    prune_dist_info: bool = False,
    exclude_patterns: Optional[list[str]] = None,
    bundled_strip_path: Optional[Path] = None,
    stage_strip_path: Optional[Path] = None,
) -> dict:
    """
    Prune non-runtime bloat and strip debug symbols from ``target`` in place.

    Emits a CloudWatch-friendly size-reduction summary and warns if the final
    uncompressed size exceeds AWS Lambda's 250 MB unzipped limit.
    """
    patterns = list(exclude_patterns) if exclude_patterns else []
    uncompressed_before = calculate_dir_size(target)

    bytecode_bytes = 0
    tests_bytes = 0
    docs_and_meta_bytes = 0
    custom_excludes_bytes = 0
    stripped_bytes = 0

    for dirpath, dirnames, filenames in os.walk(target, topdown=True, followlinks=False):
        current_dir = Path(dirpath)
        rel_dir = current_dir.relative_to(target)
        rel_parts = () if rel_dir == Path(".") else rel_dir.parts
        in_dist_info = _is_inside_dist_info(rel_parts)

        kept_dirs: list[str] = []
        for dname in dirnames:
            dpath = current_dir / dname
            if dpath.is_symlink():
                kept_dirs.append(dname)
                continue

            rel_posix = (rel_dir / dname).as_posix() if rel_dir != Path(".") else dname

            if patterns and _matches_any(rel_posix, dname, patterns):
                custom_excludes_bytes += calculate_dir_size(dpath)
                shutil.rmtree(dpath)
            elif dname in _BYTECODE_DIRS:
                bytecode_bytes += calculate_dir_size(dpath)
                shutil.rmtree(dpath)
            elif prune_dist_info and dname.endswith(_DIST_INFO_SUFFIXES):
                docs_and_meta_bytes += calculate_dir_size(dpath)
                shutil.rmtree(dpath)
            elif prune_tests and not in_dist_info and dname in _TEST_DIRS:
                tests_bytes += calculate_dir_size(dpath)
                shutil.rmtree(dpath)
            elif not in_dist_info and dname in _DOC_DIRS:
                docs_and_meta_bytes += calculate_dir_size(dpath)
                shutil.rmtree(dpath)
            else:
                kept_dirs.append(dname)

        dirnames[:] = kept_dirs

        for fname in filenames:
            fpath = current_dir / fname
            if fpath.is_symlink():
                continue

            try:
                fsize = fpath.lstat().st_size
            except OSError:
                continue

            rel_posix = (rel_dir / fname).as_posix() if rel_dir != Path(".") else fname
            suffix = fpath.suffix.lower()

            if patterns and _matches_any(rel_posix, fname, patterns):
                fpath.unlink()
                custom_excludes_bytes += fsize
            elif suffix in _BYTECODE_SUFFIXES:
                fpath.unlink()
                bytecode_bytes += fsize
            elif not in_dist_info and (
                suffix in _COMPILE_AND_STUB_SUFFIXES
                or suffix in _DOC_SUFFIXES
                or fname.lower().startswith(_DOC_FILENAMES_PREFIXES)
            ):
                fpath.unlink()
                docs_and_meta_bytes += fsize

    strip_available = False
    binaries_stripped = 0
    if strip_binaries:
        strip_bin = resolve_strip_binary(bundled_strip_path, stage_strip_path)
        if strip_bin is not None:
            strip_available = True
            binaries_stripped, stripped_bytes = strip_shared_libraries(target, strip_bin)
        else:
            print(
                "[INFO] strip binary not found on PATH; skipping ELF symbol stripping (pruning still applied)"
            )

    uncompressed_after = calculate_dir_size(target)
    saved_bytes = max(0, uncompressed_before - uncompressed_after)
    reduction_percent = (
        round((saved_bytes / uncompressed_before) * 100.0, 2)
        if uncompressed_before > 0
        else 0.0
    )

    print(
        f"[INFO] Layer slimming complete: {_fmt_mb(uncompressed_before)} MB -> "
        f"{_fmt_mb(uncompressed_after)} MB ({reduction_percent:.1f}% reduction, "
        f"saved {_fmt_mb(saved_bytes)} MB)\n"
        f"       breakdown: bytecode={_fmt_mb(bytecode_bytes)} MB, "
        f"tests={_fmt_mb(tests_bytes)} MB, "
        f"docs_meta={_fmt_mb(docs_and_meta_bytes)} MB, "
        f"stripped_so={_fmt_mb(stripped_bytes)} MB ({binaries_stripped} binaries), "
        f"custom={_fmt_mb(custom_excludes_bytes)} MB"
    )

    if uncompressed_after > LAMBDA_UNZIPPED_LIMIT_BYTES:
        print(
            f"[WARN] Unzipped layer size ({_fmt_mb(uncompressed_after)} MB) exceeds "
            f"AWS Lambda's 250 MB limit ({LAMBDA_UNZIPPED_LIMIT_BYTES:,} bytes)."
        )

    return {
        "enabled": True,
        "uncompressed_before_bytes": uncompressed_before,
        "uncompressed_after_bytes": uncompressed_after,
        "saved_bytes": saved_bytes,
        "reduction_percent": reduction_percent,
        "binaries_stripped": binaries_stripped,
        "strip_available": strip_available,
        "breakdown_bytes": {
            "bytecode": bytecode_bytes,
            "tests": tests_bytes,
            "docs_and_meta": docs_and_meta_bytes,
            "custom_excludes": custom_excludes_bytes,
            "stripped_binaries": stripped_bytes,
        },
    }
