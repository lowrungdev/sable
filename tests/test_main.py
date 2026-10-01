"""``python -m sable --check``."""

from __future__ import annotations

import os

import pytest

from sable.__main__ import main

PASSWORD = "app-password-1234"


@pytest.fixture
def minimal_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("SABLE_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SABLE_NEXTCLOUD_URL", "https://cloud.example.org")
    monkeypatch.setenv("SABLE_NEXTCLOUD_USER", "sable")
    monkeypatch.setenv("SABLE_NEXTCLOUD_PASSWORD", PASSWORD)
    monkeypatch.setenv("SABLE_STARTUP_CHECK", "false")


def test_check_prints_the_startup_warnings(minimal_env, capsys, tmp_path) -> None:
    assert main(["--check", "--env-file", str(tmp_path / "none.env")]) == 0
    out = capsys.readouterr().out
    assert "config OK" in out
    # No allow-list is the warning every bare configuration earns.
    assert "warning: SABLE_ALLOWED_ROOMS is empty" in out
    assert PASSWORD not in out


def test_check_prints_no_warning_line_when_there_is_none(
    minimal_env, monkeypatch, capsys, tmp_path
) -> None:
    monkeypatch.setenv("SABLE_ALLOWED_ROOMS", "abcd1234")
    assert main(["--check", "--env-file", str(tmp_path / "none.env")]) == 0
    assert "warning:" not in capsys.readouterr().out
