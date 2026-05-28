import importlib
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
) -> Path:
    build_root = tmp_path / "build"
    zip_base = tmp_path / "deps"
    zip_path = zip_base.with_suffix(".zip")
    monkeypatch.setattr(handler_module, "_BUILD_ROOT", build_root)
    monkeypatch.setattr(handler_module, "_ZIP_BASE", zip_base)
    monkeypatch.setattr(handler_module, "_ZIP_PATH", zip_path)
    return zip_path


def _fake_pip_install(cmd: list, check: bool, env: dict, *, fixture_files: dict):
    target = Path(cmd[cmd.index("-t") + 1])
    target.mkdir(parents=True, exist_ok=True)
    for relpath, content in fixture_files.items():
        f = target / relpath
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)

    class _Result:
        returncode = 0

    return _Result()


def test_handler_runs_pip_zips_and_uploads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_boto3: MagicMock,
    handler_module,
) -> None:
    zip_path = _patch_build_paths(monkeypatch, handler_module, tmp_path)
    monkeypatch.setenv("BUCKET_NAME", "my-bucket")
    monkeypatch.setenv("OBJECT_KEY", "deps.zip")

    captured: dict = {}

    def fake_run(cmd, check, env):
        captured["cmd"] = cmd
        captured["env"] = env
        return _fake_pip_install(
            cmd,
            check,
            env,
            fixture_files={"fakepkg/__init__.py": "OK\n"},
        )

    monkeypatch.setattr(handler_module.subprocess, "run", fake_run)

    s3_client = MagicMock(name="s3_client")
    fake_boto3.client.return_value = s3_client

    result = handler_module.handler({}, None)

    assert "--only-binary" in captured["cmd"] and ":all:" in captured["cmd"]
    assert captured["env"]["PIP_DISABLE_PIP_VERSION_CHECK"] == "1"
    assert captured["env"]["HOME"] == "/tmp"

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
    }


def test_handler_clears_stale_build_dir_on_warm_invocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_boto3: MagicMock,
    handler_module,
) -> None:
    """A pre-existing /tmp/build from a previous warm invocation must be wiped before pip install."""
    zip_path = _patch_build_paths(monkeypatch, handler_module, tmp_path)
    monkeypatch.setenv("BUCKET_NAME", "b")
    monkeypatch.setenv("OBJECT_KEY", "k")

    stale_dir = tmp_path / "build" / "python" / "stalepkg"
    stale_dir.mkdir(parents=True)
    (stale_dir / "__init__.py").write_text("STALE\n")

    def fake_run(cmd, check, env):
        return _fake_pip_install(
            cmd, check, env, fixture_files={"freshpkg/__init__.py": "FRESH\n"}
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
    _patch_build_paths(monkeypatch, handler_module, tmp_path)
    monkeypatch.setenv("BUCKET_NAME", "b")
    monkeypatch.setenv("OBJECT_KEY", "k")

    def fake_run(cmd, check, env):
        return _fake_pip_install(cmd, check, env, fixture_files={})

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
