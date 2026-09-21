import importlib
import subprocess
import sys
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def fake_boto3(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Stub `boto3` so the handler module can be imported on the dev machine."""
    fake = MagicMock(name="boto3")
    monkeypatch.setitem(sys.modules, "boto3", fake)
    return fake


@pytest.fixture
def handler_module(fake_boto3: MagicMock):
    """Re-import the handler module fresh each test so module-level patches stick."""
    sys.modules.pop("lambda_deps_builder.builder_handler.handler", None)
    return importlib.import_module("lambda_deps_builder.builder_handler.handler")


def _patch_build_paths(
    monkeypatch: pytest.MonkeyPatch, handler_module, tmp_path: Path
) -> tuple[Path, Path, Path, Path]:
    build_root = tmp_path / "build"
    zip_base = tmp_path / "deps"
    zip_path = zip_base.with_suffix(".zip")
    warm_uv_dir = tmp_path / "uv_tool"
    warm_uv_bin = warm_uv_dir / "bin" / "uv"
    stage_uv_dir = tmp_path / "uv_bin"
    stage_uv_bin = stage_uv_dir / "uv"
    bundled_uv = tmp_path / "bundled_uv"

    monkeypatch.setattr(handler_module, "_BUILD_ROOT", build_root)
    monkeypatch.setattr(handler_module, "_ZIP_BASE", zip_base)
    monkeypatch.setattr(handler_module, "_ZIP_PATH", zip_path)
    monkeypatch.setattr(handler_module, "_WARM_UV_DIR", warm_uv_dir)
    monkeypatch.setattr(handler_module, "_WARM_UV_BIN", warm_uv_bin)
    monkeypatch.setattr(handler_module, "_STAGE_UV_DIR", stage_uv_dir)
    monkeypatch.setattr(handler_module, "_STAGE_UV_BIN", stage_uv_bin)
    monkeypatch.setattr(handler_module, "_BUNDLED_UV", bundled_uv)
    return zip_path, warm_uv_bin, stage_uv_bin, bundled_uv


def _fake_run(
    cmd: list,
    check: bool,
    env: dict,
    *,
    fixture_files: dict,
    warm_uv_bin: Path,
):
    cmd_str = [str(c) for c in cmd]
    if "pip" in cmd_str and "install" in cmd_str and "-t" in cmd_str:
        target = Path(cmd_str[cmd_str.index("-t") + 1])
        target.mkdir(parents=True, exist_ok=True)
        if any("uv" in part for part in cmd_str[cmd_str.index("install") + 1 : cmd_str.index("-t")]):
            warm_uv_bin.parent.mkdir(parents=True, exist_ok=True)
            warm_uv_bin.write_text("#!/bin/sh\n")
        else:
            for relpath, content in fixture_files.items():
                f = target / relpath
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(content)
    elif "--target" in cmd_str:
        target = Path(cmd_str[cmd_str.index("--target") + 1])
        target.mkdir(parents=True, exist_ok=True)
        for relpath, content in fixture_files.items():
            f = target / relpath
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(content)

    class _Result:
        returncode = 0

    return _Result()


def test_handler_uses_uv_by_default_bootstraps_and_installs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_boto3: MagicMock,
    handler_module,
) -> None:
    """Default BUILD_ENGINE='uv' bootstraps uv via pip, then runs uv pip install."""
    zip_path, warm_uv_bin, _, _ = _patch_build_paths(monkeypatch, handler_module, tmp_path)
    monkeypatch.setenv("BUCKET_NAME", "my-bucket")
    monkeypatch.setenv("OBJECT_KEY", "deps.zip")

    invocations: list[dict] = []

    def fake_subprocess_run(cmd, check, env):
        cmd_str = [str(c) for c in cmd]
        invocations.append({"cmd": cmd_str, "env": env})
        return _fake_run(
            cmd,
            check,
            env,
            fixture_files={"fakepkg/__init__.py": "OK\n"},
            warm_uv_bin=warm_uv_bin,
        )

    monkeypatch.setattr(handler_module.subprocess, "run", fake_subprocess_run)

    s3_client = MagicMock(name="s3_client")
    fake_boto3.client.return_value = s3_client

    result = handler_module.handler({}, None)

    # Invocations: 1. pip install uv, 2. uv pip install ...
    assert len(invocations) == 2
    assert "uv" in invocations[0]["cmd"]
    assert invocations[0]["cmd"][:4] == [sys.executable, "-m", "pip", "install"]

    uv_call = invocations[1]
    assert uv_call["cmd"][0] == str(warm_uv_bin)
    assert uv_call["cmd"][1:3] == ["pip", "install"]
    assert "--only-binary" in uv_call["cmd"] and ":all:" in uv_call["cmd"]
    assert "--no-cache" in uv_call["cmd"]
    assert "--python" in uv_call["cmd"] and sys.executable in uv_call["cmd"]
    assert uv_call["env"]["HOME"] == "/tmp"
    assert uv_call["env"]["UV_CACHE_DIR"] == "/tmp/.uv_cache"

    fake_boto3.client.assert_called_once_with("s3")
    s3_client.upload_file.assert_called_once_with(
        str(zip_path), "my-bucket", "deps.zip"
    )

    assert zip_path.is_file()
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    assert any(n == "python/fakepkg/__init__.py" for n in names)
    assert all(n.startswith("python/") or n == "python/" for n in names)

    assert result == {
        "bucket": "my-bucket",
        "key": "deps.zip",
        "size": zip_path.stat().st_size,
        "engine": "uv",
    }


def test_handler_reuses_warm_uv_binary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_boto3: MagicMock,
    handler_module,
) -> None:
    """If uv is already installed in /tmp from warm container, pip install uv is skipped."""
    zip_path, warm_uv_bin, _, _ = _patch_build_paths(monkeypatch, handler_module, tmp_path)
    monkeypatch.setenv("BUCKET_NAME", "my-bucket")
    monkeypatch.setenv("OBJECT_KEY", "deps.zip")

    # Pre-create the warm uv binary
    warm_uv_bin.parent.mkdir(parents=True, exist_ok=True)
    warm_uv_bin.write_text("#!/bin/sh\n")

    invocations: list[dict] = []

    def fake_subprocess_run(cmd, check, env):
        cmd_str = [str(c) for c in cmd]
        invocations.append({"cmd": cmd_str, "env": env})
        return _fake_run(
            cmd,
            check,
            env,
            fixture_files={"freshpkg/__init__.py": "FRESH\n"},
            warm_uv_bin=warm_uv_bin,
        )

    monkeypatch.setattr(handler_module.subprocess, "run", fake_subprocess_run)
    fake_boto3.client.return_value = MagicMock()

    result = handler_module.handler({}, None)

    # Only 1 invocation (uv pip install) - no pip install uv!
    assert len(invocations) == 1
    assert invocations[0]["cmd"][0] == str(warm_uv_bin)
    assert result["engine"] == "uv"


def test_handler_uses_bundled_uv_binary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_boto3: MagicMock,
    handler_module,
) -> None:
    """If uv binary is bundled in the Lambda asset, it is copied to /tmp and used."""
    zip_path, warm_uv_bin, stage_uv_bin, bundled_uv = _patch_build_paths(
        monkeypatch, handler_module, tmp_path
    )
    monkeypatch.setenv("BUCKET_NAME", "my-bucket")
    monkeypatch.setenv("OBJECT_KEY", "deps.zip")

    # Pre-create bundled uv
    bundled_uv.parent.mkdir(parents=True, exist_ok=True)
    bundled_uv.write_text("#!/bin/sh\n")

    invocations: list[dict] = []

    def fake_subprocess_run(cmd, check, env):
        cmd_str = [str(c) for c in cmd]
        invocations.append({"cmd": cmd_str, "env": env})
        return _fake_run(
            cmd,
            check,
            env,
            fixture_files={"bundledpkg/__init__.py": "OK\n"},
            warm_uv_bin=warm_uv_bin,
        )

    monkeypatch.setattr(handler_module.subprocess, "run", fake_subprocess_run)
    fake_boto3.client.return_value = MagicMock()

    result = handler_module.handler({}, None)

    assert stage_uv_bin.is_file()
    assert len(invocations) == 1
    assert invocations[0]["cmd"][0] == str(stage_uv_bin)
    assert result["engine"] == "uv"


def test_handler_falls_back_to_pip_on_uv_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_boto3: MagicMock,
    handler_module,
) -> None:
    """When uv install fails and FALLBACK_TO_PIP is enabled, falls back to standard pip."""
    zip_path, warm_uv_bin, _, _ = _patch_build_paths(monkeypatch, handler_module, tmp_path)
    monkeypatch.setenv("BUCKET_NAME", "my-bucket")
    monkeypatch.setenv("OBJECT_KEY", "deps.zip")
    monkeypatch.setenv("FALLBACK_TO_PIP", "1")

    call_count = 0

    def fake_subprocess_run(cmd, check, env):
        nonlocal call_count
        call_count += 1
        cmd_str = [str(c) for c in cmd]
        # Fail when trying to install or run uv
        if "uv" in cmd_str:
            raise subprocess.CalledProcessError(1, cmd, output="uv error")
        # Succeed for standard pip fallback
        return _fake_run(
            cmd,
            check,
            env,
            fixture_files={"fallbackpkg/__init__.py": "OK\n"},
            warm_uv_bin=warm_uv_bin,
        )

    monkeypatch.setattr(handler_module.subprocess, "run", fake_subprocess_run)
    fake_boto3.client.return_value = MagicMock()

    result = handler_module.handler({}, None)

    assert result["engine"] == "pip"
    with zipfile.ZipFile(zip_path) as zf:
        assert "python/fallbackpkg/__init__.py" in zf.namelist()


def test_handler_raises_when_uv_fails_and_fallback_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_boto3: MagicMock,
    handler_module,
) -> None:
    """When FALLBACK_TO_PIP is '0' and uv fails, raises immediately without fallback."""
    _patch_build_paths(monkeypatch, handler_module, tmp_path)
    monkeypatch.setenv("BUCKET_NAME", "my-bucket")
    monkeypatch.setenv("OBJECT_KEY", "deps.zip")
    monkeypatch.setenv("FALLBACK_TO_PIP", "0")

    def fake_subprocess_run(cmd, check, env):
        raise subprocess.CalledProcessError(1, cmd, output="uv bootstrap failed")

    monkeypatch.setattr(handler_module.subprocess, "run", fake_subprocess_run)
    fake_boto3.client.return_value = MagicMock()

    with pytest.raises(subprocess.CalledProcessError):
        handler_module.handler({}, None)


def test_handler_respects_pip_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_boto3: MagicMock,
    handler_module,
) -> None:
    """When BUILD_ENGINE='pip', uses standard pip install directly without uv."""
    zip_path, warm_uv_bin, _, _ = _patch_build_paths(monkeypatch, handler_module, tmp_path)
    monkeypatch.setenv("BUCKET_NAME", "my-bucket")
    monkeypatch.setenv("OBJECT_KEY", "deps.zip")
    monkeypatch.setenv("BUILD_ENGINE", "pip")

    invocations: list[dict] = []

    def fake_subprocess_run(cmd, check, env):
        cmd_str = [str(c) for c in cmd]
        invocations.append({"cmd": cmd_str, "env": env})
        return _fake_run(
            cmd,
            check,
            env,
            fixture_files={"pippkg/__init__.py": "OK\n"},
            warm_uv_bin=warm_uv_bin,
        )

    monkeypatch.setattr(handler_module.subprocess, "run", fake_subprocess_run)
    fake_boto3.client.return_value = MagicMock()

    result = handler_module.handler({}, None)

    assert len(invocations) == 1
    assert invocations[0]["cmd"][:4] == [sys.executable, "-m", "pip", "install"]
    assert "-t" in invocations[0]["cmd"]
    assert result["engine"] == "pip"


def test_handler_clears_stale_build_dir_on_warm_invocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_boto3: MagicMock,
    handler_module,
) -> None:
    """A pre-existing /tmp/build from a previous warm invocation must be wiped before install."""
    zip_path, warm_uv_bin, _, _ = _patch_build_paths(monkeypatch, handler_module, tmp_path)
    monkeypatch.setenv("BUCKET_NAME", "b")
    monkeypatch.setenv("OBJECT_KEY", "k")
    monkeypatch.setenv("BUILD_ENGINE", "pip")

    stale_dir = tmp_path / "build" / "python" / "stalepkg"
    stale_dir.mkdir(parents=True)
    (stale_dir / "__init__.py").write_text("STALE\n")

    def fake_run(cmd, check, env):
        return _fake_run(
            cmd,
            check,
            env,
            fixture_files={"freshpkg/__init__.py": "FRESH\n"},
            warm_uv_bin=warm_uv_bin,
        )

    monkeypatch.setattr(handler_module.subprocess, "run", fake_run)
    fake_boto3.client.return_value = MagicMock()

    handler_module.handler({}, None)

    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
    assert "python/freshpkg/__init__.py" in names
    assert not any("stalepkg" in n for n in names), (
        f"stale package leaked into new layer: {names}"
    )


def test_handler_fails_loudly_on_empty_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_boto3: MagicMock,
    handler_module,
) -> None:
    """Empty requirements.txt => empty install => RuntimeError, not a silently empty layer."""
    _, warm_uv_bin, _, _ = _patch_build_paths(monkeypatch, handler_module, tmp_path)
    monkeypatch.setenv("BUCKET_NAME", "b")
    monkeypatch.setenv("OBJECT_KEY", "k")
    monkeypatch.setenv("BUILD_ENGINE", "pip")

    def fake_run(cmd, check, env):
        return _fake_run(cmd, check, env, fixture_files={}, warm_uv_bin=warm_uv_bin)

    monkeypatch.setattr(handler_module.subprocess, "run", fake_run)
    s3_client = MagicMock()
    fake_boto3.client.return_value = s3_client

    with pytest.raises(RuntimeError, match="produced no files"):
        handler_module.handler({}, None)
    s3_client.upload_file.assert_not_called()


def test_handler_raises_when_env_vars_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_boto3: MagicMock,
    handler_module,
) -> None:
    _patch_build_paths(monkeypatch, handler_module, tmp_path)
    monkeypatch.delenv("BUCKET_NAME", raising=False)
    monkeypatch.delenv("OBJECT_KEY", raising=False)

    with pytest.raises(KeyError):
        handler_module.handler({}, None)

