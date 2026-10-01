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
        f"the version must be MAJOR.MINOR, e.g. 1.1 (see docs/releasing.md); got {declared!r}"
    )


def test_the_package_reports_a_real_version() -> None:
    # Derived from the metadata the build backend generates out of pyproject,
    # so an installed copy cannot disagree with it. A bare "0+unknown" means
    # the package is not installed at all.
    assert MAJOR_MINOR.fullmatch(sable.__version__), (
        f"sable.__version__ is {sable.__version__!r}; install the package (pip install -e '.[dev]')"
    )


def test_pyproject_does_not_derive_the_version_from_elsewhere() -> None:
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    assert "version" not in config["project"].get("dynamic", []), (
        "the version must stay a plain field in pyproject.toml: editing it on "
        "main is what triggers a release build"
    )
    assert "version" not in config.get("tool", {}).get("hatch", {})


CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"


def changelog_section(version: str) -> str:
    """The notes under ``## <version>``, up to the next ``##`` heading.

    Mirrors the awk in .forgejo/workflows/release.yml, which turns this same
    section into the Forgejo release body.
    """
    heading = re.compile(rf"^## +{re.escape(version)}(\s.*)?$")
    lines: list[str] = []
    inside = False
    for line in CHANGELOG.read_text(encoding="utf-8").splitlines():
        if heading.match(line):
            inside = True
            continue
        if inside and line.startswith("## "):
            break
        if inside:
            lines.append(line)
    return "\n".join(lines).strip()


def test_the_changelog_documents_the_current_version() -> None:
    version = project_version()
    notes = changelog_section(version)
    assert notes, (
        f"CHANGELOG.md has no notes under '## {version}'. Write them before "
        f"releasing - CI turns that section into the release body, and refuses to "
        f"publish without it."
    )
