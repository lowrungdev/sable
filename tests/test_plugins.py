"""Plugins, without a worker: discovery, the settings schema, the registry and the settings.

Everything that needs a process (the handshake, calls, timeouts) is in
test_plugin_manager.py and test_plugin_worker.py.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from conftest import make_config
from plugin_helpers import posix_only, write_plugin
from sable.commands import Command, Registry, registry
from sable.config import Config, ConfigError
from sable.plugins import (
    MAX_ENTRY_BYTES,
    MAX_PLUGIN_FILES,
    MAX_PLUGINS,
    MAX_SETTINGS_BYTES,
    PluginManager,
    SettingsError,
    Status,
    bootstrap_source,
    check_syntax,
    discover,
    load_settings,
    worker_environment,
)

# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


def names(root: Path) -> list[str]:
    records, _ = discover(root)
    return [r.name for r in records]


def test_a_python_file_with_a_settings_file_is_a_plugin(tmp_path: Path) -> None:
    write_plugin(tmp_path, "weather")
    records, notes = discover(tmp_path)
    assert [r.name for r in records] == ["weather"]
    assert records[0].status is not Status.FAILED
    assert records[0].entry == tmp_path / "weather" / "weather.py"
    assert notes == []


def test_a_python_file_with_no_settings_file_is_a_helper_not_a_plugin(tmp_path: Path) -> None:
    folder = write_plugin(tmp_path, "weather")
    (folder / "helpers.py").write_text("y = 2\n")
    assert names(tmp_path) == ["weather"]


def test_a_settings_file_with_no_python_file_fails_that_plugin(tmp_path: Path) -> None:
    folder = tmp_path / "weather"
    folder.mkdir()
    (folder / "weather_settings.yaml").write_text("enabled: true\n")
    records, _ = discover(tmp_path)
    assert records[0].name == "weather"
    assert records[0].status is Status.FAILED
    assert "weather.py" in records[0].reason


def test_one_directory_may_hold_several_plugins(tmp_path: Path) -> None:
    write_plugin(tmp_path, "alpha", directory="pack")
    write_plugin(tmp_path, "beta", directory="pack")
    assert names(tmp_path) == ["alpha", "beta"]


def test_the_name_is_the_lowercased_stem(tmp_path: Path) -> None:
    write_plugin(tmp_path, "Weather")
    assert names(tmp_path) == ["weather"]


@pytest.mark.parametrize(
    "stem",
    ["9lives", "has space", "-dash", "dot.ted", "x" * 33, "ünï"],
)
def test_an_illegal_name_fails_the_plugin_and_is_never_trusted(tmp_path: Path, stem: str) -> None:
    write_plugin(tmp_path, stem, directory="d")
    records, _ = discover(tmp_path)
    assert records[0].status is Status.FAILED
    assert "legal plugin name" in records[0].reason
    # No slug to show: the display says so rather than echo the name as if it were one.
    assert records[0].name == ""
    assert records[0].display.startswith("(invalid name)")


def test_a_name_may_be_thirty_two_characters_with_dashes_and_underscores(tmp_path: Path) -> None:
    write_plugin(tmp_path, "a" + "b-c_d" * 6 + "e")  # 32 characters
    records, _ = discover(tmp_path)
    assert records[0].status is not Status.FAILED


def test_the_later_of_two_plugins_with_one_name_is_rejected_and_names_the_first(
    tmp_path: Path,
) -> None:
    write_plugin(tmp_path, "weather", directory="a")
    write_plugin(tmp_path, "weather", directory="b")
    first, second = discover(tmp_path)[0]
    assert first.status is not Status.FAILED
    assert first.duplicate is False
    assert second.status is Status.FAILED
    assert second.duplicate is True
    assert "'weather' is already used by a/weather.py" in second.reason
    # Told apart in lists, since both are called weather.
    assert first.display == "weather"
    assert second.display == "weather (b/weather.py)"


def test_directories_starting_with_a_dot_or_an_underscore_are_skipped(tmp_path: Path) -> None:
    write_plugin(tmp_path, "hidden", directory=".git")
    write_plugin(tmp_path, "private", directory="_wip")
    write_plugin(tmp_path, "shown", directory="ok")
    assert names(tmp_path) == ["shown"]


def test_only_immediate_subdirectories_are_scanned(tmp_path: Path) -> None:
    write_plugin(tmp_path / "outer", "deep", directory="inner")
    write_plugin(tmp_path, "top")
    assert names(tmp_path) == ["top"]


def test_files_at_the_root_are_ignored(tmp_path: Path) -> None:
    (tmp_path / "loose.py").write_text("x = 1\n")
    (tmp_path / "loose_settings.yaml").write_text("enabled: true\n")
    assert names(tmp_path) == []


def test_a_missing_directory_is_a_note_not_a_crash(tmp_path: Path) -> None:
    records, notes = discover(tmp_path / "nope")
    assert records == []
    assert "cannot read" in notes[0]


@posix_only
def test_a_directory_symlink_is_skipped_and_said(tmp_path: Path) -> None:
    real = tmp_path / "elsewhere"
    write_plugin(real, "weather")
    root = tmp_path / "root"
    root.mkdir()
    os.symlink(real / "weather", root / "linked")
    records, notes = discover(root)
    assert records == []
    assert "symlink" in notes[0]


@posix_only
def test_an_entry_file_that_is_a_symlink_is_rejected(tmp_path: Path) -> None:
    folder = write_plugin(tmp_path, "weather")
    real = tmp_path / "real.py"
    real.write_text("x = 1\n")
    (folder / "weather.py").unlink()
    os.symlink(real, folder / "weather.py")
    records, _ = discover(tmp_path)
    assert records[0].status is Status.FAILED
    assert "symlink" in records[0].reason


@posix_only
def test_a_settings_file_that_is_a_symlink_is_rejected(tmp_path: Path) -> None:
    folder = write_plugin(tmp_path, "weather")
    real = tmp_path / "real.yaml"
    real.write_text("enabled: true\n")
    (folder / "weather_settings.yaml").unlink()
    os.symlink(real, folder / "weather_settings.yaml")
    records, _ = discover(tmp_path)
    assert records[0].status is Status.FAILED
    assert "symlink" in records[0].reason


def test_an_oversize_entry_file_is_rejected(tmp_path: Path) -> None:
    write_plugin(tmp_path, "big", source="#" * (MAX_ENTRY_BYTES + 1))
    records, _ = discover(tmp_path)
    assert records[0].status is Status.FAILED
    assert "more than" in records[0].reason


def test_an_oversize_settings_file_is_rejected(tmp_path: Path) -> None:
    write_plugin(tmp_path, "big", raw_yaml="#" * (MAX_SETTINGS_BYTES + 1))
    records, _ = discover(tmp_path)
    assert records[0].status is Status.FAILED


def test_records_come_in_sorted_path_order(tmp_path: Path) -> None:
    write_plugin(tmp_path, "zeta", directory="a")
    write_plugin(tmp_path, "alpha", directory="b")
    assert names(tmp_path) == ["zeta", "alpha"]


# --------------------------------------------------------------------------- #
# The settings schema
# --------------------------------------------------------------------------- #


def settings_file(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "x_settings.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_an_empty_settings_file_is_valid_and_inactive(tmp_path: Path) -> None:
    parsed = load_settings(settings_file(tmp_path, ""))
    assert parsed.enabled is True
    assert parsed.access.rooms == []
    assert parsed.settings == {}


def test_a_full_settings_file(tmp_path: Path) -> None:
    parsed = load_settings(
        settings_file(
            tmp_path,
            "enabled: false\n"
            "access:\n  rooms: ['abcd1234', '*']\n  users: [alice, ' bob ']\n  admins_only: true\n"
            "settings:\n  city: Berlin\n  nested: {a: [1, 2.5, true, null]}\n",
        )
    )
    assert parsed.enabled is False
    assert parsed.access.rooms == ["abcd1234", "*"]
    assert parsed.access.users == ["alice", "bob"]
    assert parsed.access.admins_only is True
    assert parsed.settings["nested"] == {"a": [1, 2.5, True, None]}


@pytest.mark.parametrize(
    "text",
    [
        "surprise: 1\n",
        "access: {rooms: [], extra: 1}\n",
        "enabled: maybe\n",
        "enabled: 1\n",
        "access: {admins_only: 'true'}\n",
        "access: {rooms: abcd1234}\n",
        "access: {rooms: [Team chat]}\n",
        "access: {rooms: [ABCD1234]}\n",
        "access: {rooms: [123456789]}\n",
        "access: {users: [alice, '']}\n",
        "access: {users: [1]}\n",
        "access: banana\n",
        "settings: [1, 2]\n",
        "[1, 2]\n",
        "just text\n",
    ],
)
def test_a_settings_file_that_breaks_the_schema_is_refused(tmp_path: Path, text: str) -> None:
    with pytest.raises(SettingsError):
        load_settings(settings_file(tmp_path, text))


def test_an_unquoted_numeric_token_is_explained(tmp_path: Path) -> None:
    with pytest.raises(SettingsError) as caught:
        load_settings(settings_file(tmp_path, "access: {rooms: [12345678]}\n"))
    message = str(caught.value)
    assert "access.rooms" in message
    assert "quote it" in message
    assert 'write "12345678"' in message


def test_a_room_name_gets_the_token_hint(tmp_path: Path) -> None:
    with pytest.raises(SettingsError) as caught:
        load_settings(settings_file(tmp_path, "access: {rooms: ['Team chat']}\n"))
    assert "conversation token" in str(caught.value)
    assert "name of the room" in str(caught.value)


@pytest.mark.parametrize(
    "text",
    [
        "settings: {when: 2024-01-01}\n",
        "settings: {when: 2024-01-01 10:00:00}\n",
        "settings: {blob: !!binary aGVsbG8=}\n",
        "settings: {s: !!set {a, b}}\n",
        "settings: {x: .nan}\n",
        "settings: {x: .inf}\n",
        "settings: {1: one}\n",
    ],
)
def test_settings_must_be_plain_json_values(tmp_path: Path, text: str) -> None:
    with pytest.raises(SettingsError):
        load_settings(settings_file(tmp_path, text))


def test_settings_nested_too_deep_are_refused(tmp_path: Path) -> None:
    deep = "x"
    for _ in range(40):
        deep = "{a: " + deep + "}"
    with pytest.raises(SettingsError):
        load_settings(settings_file(tmp_path, f"settings: {deep}\n"))


def test_yaml_aliases_are_refused_because_they_expand(tmp_path: Path) -> None:
    text = "settings:\n  a: &a [1, 2, 3]\n  b: *a\n"
    with pytest.raises(SettingsError) as caught:
        load_settings(settings_file(tmp_path, text))
    assert "alias" in str(caught.value)


def test_a_yaml_bomb_is_refused_without_expanding_it(tmp_path: Path) -> None:
    lines = ['l0: &l0 ["ha","ha","ha","ha","ha","ha","ha","ha","ha"]']
    for i in range(1, 9):
        refs = ",".join([f"*l{i - 1}"] * 9)
        lines.append(f"l{i}: &l{i} [{refs}]")
    text = "settings:\n" + "\n".join("  " + line for line in lines) + "\n"
    with pytest.raises(SettingsError):
        load_settings(settings_file(tmp_path, text))


def test_python_tags_are_not_executed(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    text = f"settings: !!python/object/apply:pathlib.Path.write_text [{marker!s}, boo]\n"
    with pytest.raises(SettingsError):
        load_settings(settings_file(tmp_path, text))
    assert not marker.exists()


def test_more_than_one_yaml_document_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SettingsError):
        load_settings(settings_file(tmp_path, "enabled: true\n---\nenabled: false\n"))


def test_a_settings_file_that_is_not_utf8_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "x_settings.yaml"
    path.write_bytes(b"settings: {a: \xff\xfe}\n")
    with pytest.raises(SettingsError):
        load_settings(path)


def test_errors_about_the_settings_never_quote_their_values(tmp_path: Path) -> None:
    secret = "hunter2-SECRET-VALUE"
    broken = [
        f"settings: {{token: {secret}\n",  # unterminated flow mapping
        f"access: {{rooms: {secret}}}\n",  # wrong type
        f"unknown_key: {secret}\n",
        f"settings:\n  token: {secret}\n  when: 2024-01-01\n",
        f"settings:\n\ttoken: {secret}\n",  # a tab
        f"enabled: {secret}\n",
    ]
    for text in broken:
        with pytest.raises(SettingsError) as caught:
            load_settings(settings_file(tmp_path, text))
        assert secret not in str(caught.value), text


@posix_only
def test_a_fifo_in_place_of_a_settings_file_is_refused_without_blocking(tmp_path: Path) -> None:
    path = tmp_path / "x_settings.yaml"
    os.mkfifo(path)
    with pytest.raises(SettingsError):
        load_settings(path)


# --------------------------------------------------------------------------- #
# The syntax check, which never executes anything
# --------------------------------------------------------------------------- #


def test_the_syntax_check_accepts_python(tmp_path: Path) -> None:
    path = tmp_path / "p.py"
    path.write_text("async def f():\n    return 1\n")
    assert check_syntax(path) is None


def test_the_syntax_check_names_the_line(tmp_path: Path) -> None:
    path = tmp_path / "p.py"
    path.write_text("x = 1\ndef broken(:\n")
    problem = check_syntax(path)
    assert problem is not None
    assert "line 2" in problem


def test_the_syntax_check_does_not_run_the_code(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    path = tmp_path / "p.py"
    path.write_text(f"open({str(marker)!r}, 'w').write('x')\n")
    assert check_syntax(path) is None
    assert not marker.exists()


def test_the_syntax_check_survives_null_bytes_and_depth(tmp_path: Path) -> None:
    path = tmp_path / "p.py"
    path.write_bytes(b"x = 1\x00\n")
    assert check_syntax(path) is not None
    path.write_text("x = " + "(" * 5000 + "1" + ")" * 5000 + "\n")
    # Whether this parses is the interpreter's affair; it must not raise.
    check_syntax(path)


# --------------------------------------------------------------------------- #
# The registry
# --------------------------------------------------------------------------- #


async def _noop(ctx):  # pragma: no cover - never called
    return None


def test_a_registry_copy_is_independent() -> None:
    original = Registry()
    original.command("one", aliases=("uno",))(_noop)
    copy = original.copy()
    copy.add(Command("two", _noop))
    copy.command("three")(_noop)
    assert original.get("two") is None
    assert original.get("three") is None
    assert copy.get("uno") is not None
    # The commands themselves are copies too.
    copy.get("one").help = "changed"  # type: ignore[union-attr]
    assert original.get("one").help == ""  # type: ignore[union-attr]


def test_add_refuses_a_name_or_an_alias_already_taken() -> None:
    reg = Registry()
    reg.add(Command("one", _noop, aliases=("uno",)))
    with pytest.raises(ValueError, match="already registered"):
        reg.add(Command("one", _noop))
    with pytest.raises(ValueError, match="already registered"):
        reg.add(Command("two", _noop, aliases=("uno",)))
    with pytest.raises(ValueError, match="already registered"):
        reg.add(Command("uno", _noop))


def test_the_decorator_still_refuses_duplicates() -> None:
    reg = Registry()
    reg.command("one")(_noop)
    with pytest.raises(ValueError, match="already registered"):
        reg.command("ONE")(_noop)


async def test_a_bot_works_on_a_copy_of_the_registry(bot) -> None:
    assert bot.registry is not registry
    bot.registry.add(Command("only-here", _noop))
    assert registry.get("only-here") is None


def test_a_bot_given_a_registry_works_on_a_copy_of_that_one() -> None:
    mine = Registry()
    mine.command("custom")(_noop)
    from sable.bot import Bot

    first = Bot(make_config(), command_registry=mine)
    second = Bot(make_config(), command_registry=mine)
    first.registry.add(Command("extra", _noop))
    assert first.registry.get("custom") is not None
    assert second.registry.get("extra") is None
    assert mine.get("extra") is None


def test_the_plugins_command_is_a_builtin_in_the_registry() -> None:
    command = registry.get("plugins")
    assert command is not None
    assert command.plugin == ""


# --------------------------------------------------------------------------- #
# The worker's environment and bootstrap
# --------------------------------------------------------------------------- #


def test_the_environment_is_built_from_nothing() -> None:
    parent = {
        "SABLE_NEXTCLOUD_PASSWORD": "pw",
        "SABLE_LLM_API_KEY": "key",
        "HOME": "/home/x",
        "PYTHONPATH": "/evil",
        "LD_PRELOAD": "/evil.so",
        "PATH": "/evil/bin",
        "AWS_SECRET_ACCESS_KEY": "s",
    }
    env = worker_environment("", parent)
    assert env == {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "TZ": "UTC",
    }


def test_the_environment_carries_the_certificate_locations_and_the_zone() -> None:
    parent = {"SSL_CERT_FILE": "/etc/ssl/ca.crt", "SSL_CERT_DIR": "/etc/ssl/certs", "X": "1"}
    env = worker_environment("Europe/Berlin", parent)
    assert env["SSL_CERT_FILE"] == "/etc/ssl/ca.crt"
    assert env["SSL_CERT_DIR"] == "/etc/ssl/certs"
    assert env["TZ"] == "Europe/Berlin"
    assert "X" not in env


def test_the_bootstrap_sets_the_documented_limits_and_hands_over_to_the_host() -> None:
    source = bootstrap_source("/pkg/root", 300)
    compile(source, "<bootstrap>", "exec")
    for needle in ("RLIMIT_AS", "RLIMIT_NOFILE", "RLIMIT_CORE", "RLIMIT_CPU", "'/pkg/root'"):
        assert needle in source
    assert "from sable.plugin_host import main" in source


# --------------------------------------------------------------------------- #
# The settings
# --------------------------------------------------------------------------- #


@pytest.fixture
def plugin_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    for key in list(os.environ):
        if key.startswith("SABLE_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SABLE_NEXTCLOUD_URL", "https://cloud.example.org")
    monkeypatch.setenv("SABLE_NEXTCLOUD_USER", "sable")
    monkeypatch.setenv("SABLE_NEXTCLOUD_PASSWORD", "app-password-1234")
    return tmp_path


def test_the_plugins_are_off_by_default(plugin_env: Path) -> None:
    config = Config.from_env()
    assert config.plugins_dir == ""
    assert config.plugins_timeout == 30
    assert config.plugins_strict is False


@posix_only
def test_the_plugins_directory_is_read_from_the_environment(
    plugin_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SABLE_PLUGINS_DIR", str(plugin_env))
    monkeypatch.setenv("SABLE_PLUGINS_TIMEOUT", "45")
    monkeypatch.setenv("SABLE_PLUGINS_STRICT", "true")
    config = Config.from_env()
    assert config.plugins_dir == str(plugin_env)
    assert config.plugins_timeout == 45
    assert config.plugins_strict is True


@posix_only
def test_a_missing_plugins_directory_is_a_config_error(
    plugin_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SABLE_PLUGINS_DIR", str(plugin_env / "nope"))
    with pytest.raises(ConfigError, match="SABLE_PLUGINS_DIR"):
        Config.from_env()


@posix_only
def test_a_plugins_path_that_is_a_file_is_a_config_error(
    plugin_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    afile = plugin_env / "afile"
    afile.write_text("x")
    monkeypatch.setenv("SABLE_PLUGINS_DIR", str(afile))
    with pytest.raises(ConfigError, match="not a directory"):
        Config.from_env()


@pytest.mark.parametrize("value", ["0", "-5", "601", "9999"])
def test_the_plugin_timeout_is_one_to_six_hundred(
    plugin_env: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("SABLE_PLUGINS_TIMEOUT", value)
    with pytest.raises(ConfigError, match="SABLE_PLUGINS_TIMEOUT"):
        Config.from_env()


@pytest.mark.parametrize("value", ["1", "600"])
def test_the_edges_of_the_plugin_timeout_are_allowed(
    plugin_env: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("SABLE_PLUGINS_TIMEOUT", value)
    assert Config.from_env().plugins_timeout == int(value)


def test_a_timeout_that_is_not_a_number_is_a_config_error(
    plugin_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SABLE_PLUGINS_TIMEOUT", "soon")
    with pytest.raises(ConfigError, match="integer"):
        Config.from_env()


def test_plugins_are_refused_off_posix_and_the_reason_is_named(
    plugin_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SABLE_PLUGINS_DIR", str(plugin_env))
    monkeypatch.setattr("sable.config.os.name", "nt")
    with pytest.raises(ConfigError, match="process groups"):
        Config.from_env()


@posix_only
def test_a_writable_plugins_directory_warns(
    plugin_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.geteuid() == 0:
        pytest.skip("root can always write")
    monkeypatch.setenv("SABLE_PLUGINS_DIR", str(plugin_env))
    config = Config.from_env()
    assert any("read-only" in w and "writable" in w for w in config.warnings)


@posix_only
def test_a_read_only_plugins_directory_does_not_warn(
    plugin_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.geteuid() == 0:
        pytest.skip("root can write anywhere")
    folder = plugin_env / "ro"
    folder.mkdir()
    folder.chmod(0o555)
    monkeypatch.setenv("SABLE_PLUGINS_DIR", str(folder))
    try:
        config = Config.from_env()
    finally:
        folder.chmod(0o755)
    assert not any("writable" in w for w in config.warnings)


def test_strict_without_a_directory_warns_that_it_does_nothing(
    plugin_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SABLE_PLUGINS_STRICT", "true")
    config = Config.from_env()
    assert any("SABLE_PLUGINS_STRICT has no effect" in w for w in config.warnings)


# --------------------------------------------------------------------------- #
# Settings that cannot be read, and said kindly when they can be fixed
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text",
    [
        "settings: {n: " + "9" * 5000 + "}\n",
        "settings: {d: 2001-13-45}\n",
        "settings: {d: 2025-02-30}\n",
        "settings: {t: 2001-12-14t21:59:43.10-99:99}\n",
        "settings: {t: 2001-12-14 21:59:43 +99:99}\n",
    ],
    ids=["huge-integer", "month-13", "feb-30", "offset-99", "offset-99-spaced"],
)
def test_values_that_cannot_exist_are_a_settings_error_not_a_crash(tmp_path, text) -> None:
    with pytest.raises(SettingsError) as caught:
        load_settings(settings_file(tmp_path, text))
    assert "out of range or cannot exist" in str(caught.value)


@pytest.mark.parametrize(
    "text",
    [
        "x: " + "[" * 30000 + "]" * 30000,
        "x: " + "{a: " * 8000 + "1" + "}" * 8000,
        "- " * 30000 + "x\n",
        "? " * 30000 + "x\n",
    ],
    ids=["brackets", "braces", "dashes", "question-marks"],
)
def test_pathological_nesting_is_refused_before_the_parser_sees_it(tmp_path, text) -> None:
    import time

    started = time.monotonic()
    with pytest.raises(SettingsError, match="levels deep"):
        load_settings(settings_file(tmp_path, text))
    assert time.monotonic() - started < 1.0


def test_ordinary_nesting_is_fine(tmp_path) -> None:
    nested = "{a: " * 10 + "1" + "}" * 10
    assert load_settings(settings_file(tmp_path, f"settings: {nested}\n")).settings


def test_a_bare_access_or_settings_key_is_empty_not_an_error(tmp_path) -> None:
    parsed = load_settings(settings_file(tmp_path, "access:\nsettings:\n"))
    assert parsed.access.rooms == []
    assert parsed.settings == {}
    parsed = load_settings(settings_file(tmp_path, "access:\n  rooms: [abcd1234]\n  users:\n"))
    assert parsed.access.users == []


@pytest.mark.parametrize("key", ["access", "settings"])
def test_a_section_that_is_not_a_mapping_says_so_without_the_schemas_class_names(
    tmp_path, key
) -> None:
    with pytest.raises(SettingsError) as caught:
        load_settings(settings_file(tmp_path, f"{key}: banana\n"))
    message = str(caught.value)
    assert key in message
    assert "must be a mapping" in message
    assert "AccessSettings" not in message
    assert "PluginSettings" not in message


def test_yaml_booleans_in_the_users_list_get_a_hint(tmp_path) -> None:
    with pytest.raises(SettingsError) as caught:
        load_settings(settings_file(tmp_path, "access: {users: [yes, no]}\n"))
    message = str(caught.value)
    assert "access.users" in message
    assert "YAML boolean" in message
    assert "quote it" in message


def test_a_number_in_the_users_list_gets_the_same_hint_as_a_room(tmp_path) -> None:
    with pytest.raises(SettingsError, match='write "1234"'):
        load_settings(settings_file(tmp_path, "access: {users: [1234]}\n"))


# --------------------------------------------------------------------------- #
# Discovery, continued
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "filename",
    [
        "weather_settings.yml",
        "weather_setting.yaml",
        "Weather_Settings.yaml",
        "weather_setting.yml",
    ],
)
def test_a_file_that_looks_like_a_settings_file_is_warned_about(tmp_path, filename) -> None:
    folder = tmp_path / "weather"
    folder.mkdir()
    (folder / "weather.py").write_text("x = 1\n")
    (folder / filename).write_text("access: {rooms: [abcd1234]}\n")
    records, notes = discover(tmp_path)
    assert records == []
    assert len(notes) == 1
    assert f"weather/{filename}" in notes[0]
    assert "<name>_settings.yaml" in notes[0]
    assert "ignored" in notes[0]


def test_the_exact_spelling_is_still_the_only_one_accepted(tmp_path) -> None:
    write_plugin(tmp_path, "ok")
    (tmp_path / "ok" / "other_setting.yaml").write_text("x: 1\n")
    records, notes = discover(tmp_path)
    assert [r.name for r in records] == ["ok"]
    assert len(notes) == 1


def test_directories_are_cut_in_name_order_not_in_listing_order(tmp_path) -> None:
    folder = tmp_path / "pack"
    folder.mkdir()
    for number in range(300):
        (folder / f"f{number:03d}.txt").write_text("x")
    # 'p' sorts after every 'f' file, so it is past the first 256 in name order.
    (folder / "late.py").write_text("x = 1\n")
    (folder / "late_settings.yaml").write_text("x: 1\n")
    records, notes = discover(tmp_path)
    assert records == []
    assert any("only the first 256, in name order" in note for note in notes)


def test_what_is_inside_the_cut_is_still_found(tmp_path) -> None:
    folder = tmp_path / "pack"
    folder.mkdir()
    (folder / "aaa.py").write_text("x = 1\n")
    (folder / "aaa_settings.yaml").write_text("x: 1\n")
    for number in range(300):
        (folder / f"z{number:03d}.txt").write_text("x")
    records, notes = discover(tmp_path)
    assert [r.name for r in records] == ["aaa"]
    assert notes


def test_the_plugins_directory_is_made_absolute(tmp_path, monkeypatch) -> None:
    write_plugin(tmp_path / "plugins", "weather")
    monkeypatch.chdir(tmp_path)
    records, _ = discover(Path("plugins"))
    assert records[0].entry is not None
    assert records[0].entry.is_absolute()


# --------------------------------------------------------------------------- #
# compose.yaml
# --------------------------------------------------------------------------- #


def test_compose_does_not_run_an_init_process() -> None:
    # An init would be PID 1 with the container's whole environment, readable by the
    # same-uid plugin workers. sable has to be PID 1 itself: see docs/security.md.
    text = (Path(__file__).resolve().parent.parent / "compose.yaml").read_text(encoding="utf-8")
    assert "\n    init: true\n" not in text, (
        "compose.yaml sets init: true, which puts the secrets in a PID 1 that plugins can "
        "read. Remove it; the trade-off is explained in docs/security.md "
        "(plugins and the process boundary)."
    )


# --------------------------------------------------------------------------- #
# The cap counts plugins that would be started (further, with a real handshake,
# in test_plugin_manager.py: only a plugin that actually loads should count or be
# counted against, whatever stage a DIFFERENT plugin failed at - see L7).
# --------------------------------------------------------------------------- #


def scanned(root: Path) -> PluginManager:
    """Discovery and the checks that need no worker, which is where the cap is applied."""
    manager = PluginManager(make_config(plugins_dir=str(root)))
    manager.records, manager.notes = discover(root)
    manager._prepare_all()
    return manager


def test_inactive_and_disabled_plugins_take_no_place(tmp_path) -> None:
    for number in range(10):
        write_plugin(tmp_path, f"a{number:02d}", rooms=None)  # inactive
    for number in range(5):
        write_plugin(tmp_path, f"b{number:02d}", enabled=False)
    for number in range(MAX_PLUGINS):
        write_plugin(tmp_path, f"c{number:02d}")
    manager = scanned(tmp_path)
    counts = dict.fromkeys(Status, 0)
    for record in manager.records:
        counts[record.status] += 1
    assert counts[Status.ACTIVE] == MAX_PLUGINS
    assert counts[Status.INACTIVE] == 10
    assert counts[Status.DISABLED] == 5
    assert counts[Status.FAILED] == 0


def test_a_failed_settings_file_takes_no_place_either(tmp_path) -> None:
    for number in range(5):
        write_plugin(tmp_path, f"a{number}", raw_yaml="surprise: true\n")
    for number in range(MAX_PLUGINS):
        write_plugin(tmp_path, f"p{number:02d}")
    manager = scanned(tmp_path)
    assert sum(r.status is Status.ACTIVE for r in manager.records) == MAX_PLUGINS


def crowded(root: Path, total: int) -> None:
    """``total`` plugins broken at the structure stage, spread over directories (a
    directory is only read up to 256 files)."""
    for number in range(total):
        folder = root / f"d{number // 90:02d}"
        folder.mkdir(exist_ok=True)
        (folder / f"x{number:03d}_settings.yaml").write_text("x: 1\n")


def test_the_scan_is_bounded_and_says_what_it_did_not_read(tmp_path) -> None:
    crowded(tmp_path, MAX_PLUGIN_FILES + 14)
    records, _ = discover(tmp_path)
    assert len(records) == MAX_PLUGIN_FILES + 1
    assert records[0].label == "d00/x000.py"
    last = records[-1]
    assert last.status is Status.FAILED
    assert last.display == "(and 14 more settings files)"
    assert f"at most {MAX_PLUGIN_FILES}" in last.reason
    assert "found 14 more" in last.reason


def test_a_scan_within_the_bound_has_no_overflow_record(tmp_path) -> None:
    crowded(tmp_path, MAX_PLUGIN_FILES)
    records, _ = discover(tmp_path)
    assert len(records) == MAX_PLUGIN_FILES
    assert not any(r.label.startswith("(and") for r in records)
