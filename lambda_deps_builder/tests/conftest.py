from pathlib import Path

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "e2e: deploys real AWS resources via cdk; opt in via `pytest -m e2e`",
    )


@pytest.fixture
def tmp_requirements_file(tmp_path: Path) -> Path:
    """A real requirements.txt file on disk for the construct to stage."""
    f = tmp_path / "requirements.txt"
    f.write_text("requests==2.32.3\n")
    return f
