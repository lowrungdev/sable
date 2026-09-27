"""The version is set in exactly one place: pyproject.toml."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import sable

MAJOR_MINOR = re.compile(r"\d+\.\d+")
PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def project_version() -> str:
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return config["project"]["version"]


def test_pyproject_version_is_major_minor() -> None:
    declared = project_version()
    assert MAJOR_MINOR.fullmatch(declared), (
        f"the version must be MAJOR.MINOR, e.g. 1.1 (see docs/releasing.md); "
        f"got {declared!r}"
    )


def test_the_package_reports_a_real_version() -> None:
    # Derived from the metadata the build backend generates out of pyproject,
    # so an installed copy cannot disagree with it. A bare "0+unknown" means
    # the package is not installed at all.
    assert MAJOR_MINOR.fullmatch(sable.__version__), (
        f"sable.__version__ is {sable.__version__!r}; install the package "
        f"(pip install -e '.[dev]')"
    )


def test_pyproject_does_not_derive_the_version_from_elsewhere() -> None:
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    assert "version" not in config["project"].get("dynamic", []), (
        "the version must stay a plain field in pyproject.toml: editing it on "
        "main is what triggers a release build"
    )
    assert "version" not in config.get("tool", {}).get("hatch", {})
