"""sable - a Nextcloud Talk assistant that runs as an ordinary user account."""

from __future__ import annotations

from typing import Any

__all__ = ["__version__"]

#: Declared for type checkers; the value is computed on first use (see
#: ``__getattr__``). Reading the package metadata imports importlib.metadata, which
#: costs more than the rest of ``sable.plugin_host`` together, and every plugin
#: worker starts by importing ``sable``.
__version__: str


def __getattr__(name: str) -> Any:
    if name != "__version__":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as distribution_version

    try:
        #: Read from the installed package metadata, which the build backend fills
        #: in from `version` in pyproject.toml - the one place the version is set.
        #: Note that an editable install caches this at install time: after bumping
        #: pyproject.toml, `pip install -e .` again to see the new number locally.
        value = distribution_version("sable")
    except PackageNotFoundError:  # a source tree that was never installed
        value = "0+unknown"
    globals()["__version__"] = value  # later lookups skip this function
    return value
