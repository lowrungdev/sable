"""sable - a Nextcloud Talk assistant that runs as an ordinary user account."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _distribution_version

__all__ = ["__version__"]

try:
    #: Read from the installed package metadata, which the build backend fills
    #: in from `version` in pyproject.toml - the one place the version is set.
    #: Note that an editable install caches this at install time: after bumping
    #: pyproject.toml, `pip install -e .` again to see the new number locally.
    __version__ = _distribution_version("sable")
except PackageNotFoundError:  # a source tree that was never installed
    __version__ = "0+unknown"
