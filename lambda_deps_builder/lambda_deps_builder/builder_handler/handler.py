import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import boto3

try:
    from . import analyzer
except ImportError:  # pragma: no cover - flat Lambda /var/task layout
    import analyzer  # type: ignore[no-redef]

_REQUIREMENTS_FILE = Path("/var/task/requirements.txt")
_BUILD_ROOT = Path("/tmp/build")
_ZIP_BASE = Path("/tmp/deps")
_ZIP_PATH = _ZIP_BASE.with_suffix(".zip")
_REPORT_PATH = Path("/tmp/layer-analysis.html")

_BUNDLED_UV = Path("/var/task/bin/uv")
_WARM_UV_DIR = Path("/tmp/uv_tool")
_WARM_UV_BIN = _WARM_UV_DIR / "bin" / "uv"
_STAGE_UV_DIR = Path("/tmp/uv_bin")
_STAGE_UV_BIN = _STAGE_UV_DIR / "uv"


def _resolve_uv_binary(uv_package_spec: str) -> Path:
    """
    Locate or bootstrap the `uv` executable.

    1. Checks if `uv` is bundled in the staged Lambda asset (`_BUNDLED_UV`).
       If present, copies it to `/tmp/uv_bin/uv` and sets executable permissions.
    2. Checks if `uv` was already installed in `/tmp` from a previous warm invocation.
    3. If not found, bootstraps `uv` via `pip install <uv_package_spec>` into `/tmp/uv_tool`.
    """
    if _BUNDLED_UV.is_file():
        if not _STAGE_UV_BIN.is_file():
            _STAGE_UV_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copy(_BUNDLED_UV, _STAGE_UV_BIN)
            try:
                _STAGE_UV_BIN.chmod(0o755)
            except OSError:
                pass
        return _STAGE_UV_BIN

    if _WARM_UV_BIN.is_file():
        return _WARM_UV_BIN

    win_uv = _WARM_UV_DIR / "Scripts" / "uv.exe"
    if win_uv.is_file():
        return win_uv

    _WARM_UV_DIR.mkdir(parents=True, exist_ok=True)
    pip_env = {
        **os.environ,
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "HOME": "/tmp",
    }
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            uv_package_spec,
            "-t",
            str(_WARM_UV_DIR),
            "--no-cache-dir",
            "--only-binary",
            ":all:",
        ],
        check=True,
        env=pip_env,
    )

    if _WARM_UV_BIN.is_file():
        try:
            _WARM_UV_BIN.chmod(0o755)
        except OSError:
            pass
        return _WARM_UV_BIN

    if win_uv.is_file():
        return win_uv

    for cand in _WARM_UV_DIR.glob("**/uv*"):
        if cand.is_file() and cand.stem == "uv":
            try:
                cand.chmod(0o755)
            except OSError:
                pass
            return cand

    raise RuntimeError(f"uv executable not found in {_WARM_UV_DIR} after pip install")


def _run_uv_install(uv_bin: Path, target: Path) -> None:
    """Run `uv pip install` inside Lambda with matching Python runtime."""
    uv_env = {
        **os.environ,
        "HOME": "/tmp",
        "UV_CACHE_DIR": "/tmp/.uv_cache",
    }
    subprocess.run(
        [
            str(uv_bin),
            "pip",
            "install",
            "-r",
            str(_REQUIREMENTS_FILE),
            "--target",
            str(target),
            "--python",
            sys.executable,
            "--no-cache",
            "--only-binary",
            ":all:",
        ],
        check=True,
        env=uv_env,
    )


def _run_pip_install(target: Path) -> None:
    """Run standard `pip install` inside Lambda."""
    pip_env = {
        **os.environ,
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "HOME": "/tmp",
    }
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "-r",
            str(_REQUIREMENTS_FILE),
            "-t",
            str(target),
            "--no-cache-dir",
            "--only-binary",
            ":all:",
        ],
        check=True,
        env=pip_env,
    )


def handler(event, context):
    """
    Lambda entry point. Installs the bundled requirements.txt into a layer-shaped
    directory, zips it, and uploads to S3 at the bucket/key supplied via environment.

    :param event: Lambda invocation event (unused).
    :param context: Lambda context (unused).
    :return: `{"bucket": ..., "key": ..., "size": <bytes>, "engine": <str>}`.
    """
    bucket_name = os.environ["BUCKET_NAME"]
    object_key = os.environ["OBJECT_KEY"]
    build_engine = os.environ.get("BUILD_ENGINE", "uv").lower()
    uv_package_spec = os.environ.get("UV_PACKAGE_SPEC", "uv")
    fallback_to_pip = os.environ.get("FALLBACK_TO_PIP", "1").lower() in ("1", "true", "yes")
    generate_size_report = os.environ.get("GENERATE_SIZE_REPORT", "0").lower() in (
        "1",
        "true",
        "yes",
    )
    report_object_key = os.environ.get("REPORT_OBJECT_KEY", "")
    target_runtime = os.environ.get("TARGET_RUNTIME", "")
    target_architecture = os.environ.get("TARGET_ARCHITECTURE", "")

    start_time = time.monotonic()

    # Warm-container reuse can leave previous installs on /tmp; start clean so the
    # produced zip reflects only the current requirements.txt.
    if _BUILD_ROOT.exists():
        shutil.rmtree(_BUILD_ROOT)
    target = _BUILD_ROOT / "python"
    target.mkdir(parents=True)

    used_engine = build_engine
    if build_engine == "uv":
        try:
            uv_bin = _resolve_uv_binary(uv_package_spec)
            _run_uv_install(uv_bin, target)
        except Exception as e:
            if fallback_to_pip:
                print(f"[WARN] uv build failed ({e}); falling back to standard pip install")
                if target.exists():
                    shutil.rmtree(target)
                target.mkdir(parents=True)
                _run_pip_install(target)
                used_engine = "pip"
            else:
                raise
    elif build_engine == "pip":
        _run_pip_install(target)
    else:
        raise ValueError(f"Unsupported BUILD_ENGINE: {build_engine}")

    if not any(target.iterdir()):
        raise RuntimeError(
            "pip install produced no files; requirements.txt is empty or matched no packages"
        )

    should_report = generate_size_report and bool(report_object_key)
    analysis = analyzer.analyze_directory(target) if should_report else None

    shutil.make_archive(str(_ZIP_BASE), "zip", root_dir=str(_BUILD_ROOT), base_dir="python")
    zip_size = _ZIP_PATH.stat().st_size

    s3_client = boto3.client("s3")
    s3_client.upload_file(str(_ZIP_PATH), bucket_name, object_key)

    result = {
        "bucket": bucket_name,
        "key": object_key,
        "size": zip_size,
        "engine": used_engine,
    }

    if should_report and analysis is not None:
        duration_sec = round(time.monotonic() - start_time, 2)
        html_report = analyzer.render_treemap_html(
            analysis,
            zip_bytes=zip_size,
            metadata={
                "runtime": target_runtime,
                "architecture": target_architecture,
                "engine": used_engine,
                "build_duration_sec": duration_sec,
                "bucket": bucket_name,
                "object_key": object_key,
            },
        )
        _REPORT_PATH.write_text(html_report, encoding="utf-8")
        s3_client.upload_file(
            str(_REPORT_PATH),
            bucket_name,
            report_object_key,
            ExtraArgs={"ContentType": "text/html; charset=utf-8"},
        )
        unzipped_mb = analysis["total_unzipped_bytes"] / (1024 * 1024)
        zipped_mb = zip_size / (1024 * 1024)
        print(
            f"[INFO] Layer size: {unzipped_mb:.2f} MB unzipped "
            f"({analysis['quota_percent']}% of 250 MB limit), "
            f"{zipped_mb:.2f} MB zipped across {analysis['package_count']} packages. "
            f"Report: s3://{bucket_name}/{report_object_key} "
            f"(download: aws s3 cp s3://{bucket_name}/{report_object_key} ./layer-analysis.html)"
        )
        result["report_key"] = report_object_key
        result["unzipped_size"] = analysis["total_unzipped_bytes"]
        result["package_count"] = analysis["package_count"]

    return result
