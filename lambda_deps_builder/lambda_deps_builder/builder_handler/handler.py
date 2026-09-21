import os
import shutil
import subprocess
import sys
from pathlib import Path

import boto3

_REQUIREMENTS_FILE = Path("/var/task/requirements.txt")
_BUILD_ROOT = Path("/tmp/build")
_ZIP_BASE = Path("/tmp/deps")
_ZIP_PATH = _ZIP_BASE.with_suffix(".zip")

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

    shutil.make_archive(str(_ZIP_BASE), "zip", root_dir=str(_BUILD_ROOT), base_dir="python")

    boto3.client("s3").upload_file(str(_ZIP_PATH), bucket_name, object_key)

    return {
        "bucket": bucket_name,
        "key": object_key,
        "size": _ZIP_PATH.stat().st_size,
        "engine": used_engine,
    }
