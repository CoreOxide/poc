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


def handler(event, context):
    """
    Lambda entry point. Installs the bundled requirements.txt into a layer-shaped
    directory, zips it, and uploads to S3 at the bucket/key supplied via environment.

    :param event: Lambda invocation event (unused).
    :param context: Lambda context (unused).
    :return: `{"bucket": ..., "key": ..., "size": <bytes>}`.
    """
    bucket_name = os.environ["BUCKET_NAME"]
    object_key = os.environ["OBJECT_KEY"]

    # Warm-container reuse can leave previous installs on /tmp; start clean so the
    # produced zip reflects only the current requirements.txt.
    if _BUILD_ROOT.exists():
        shutil.rmtree(_BUILD_ROOT)
    target = _BUILD_ROOT / "python"
    target.mkdir(parents=True)

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
    }
