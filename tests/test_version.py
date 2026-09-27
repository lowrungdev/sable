"""The version lives in exactly one place and follows MAJOR.MINOR."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import sable

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def test_version_is_major_minor() -> None:
    assert re.fullmatch(r"\d+\.\d+", sable.__version__), (
        f"the version must be MAJOR.MINOR, e.g. 1.1 (see docs/releasing.md); "
        f"got {sable.__version__!r}"
    )


def test_packaging_reads_the_version_from_the_package() -> None:
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    project = config["project"]
    assert "version" not in project, (
        "pyproject.toml must not hardcode a version; it is derived from "
        "sable.__version__ so the two cannot drift"
    )
    assert "version" in project["dynamic"]
    assert config["tool"]["hatch"]["version"]["path"] == "src/sable/__init__.py"
