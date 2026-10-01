"""Rendering somebody else's webhook payload into a chat message."""

from __future__ import annotations

import pytest

from sable.hooks import (
    dedupe,
    flatten,
    is_noise,
    render,
    render_with_template,
    words,
)

KOMODO = {
    "_id": "66f1a2b3",
    "ts": 1727441234000,
    "resolved": False,
    "level": "CRITICAL",
    "target": {"type": "Stack", "id": "66a1"},
    "data": {
        "type": "StackStateChange",
        "data": {
            "id": "66a1",
            "name": "sable",
            "server_id": "66b2",
            "server_name": "prod-1",
            "from": "Running",
            "to": "Unhealthy",
        },
    },
    "resolved_ts": None,
}

ALERTMANAGER = {
    "receiver": "sable",
    "status": "firing",
    "alerts": [
        {
            "status": "firing",
            "labels": {"alertname": "DiskFull", "severity": "critical", "instance": "db01"},
            "annotations": {
                "summary": "Disk almost full on db01",
                "description": "98% of /var used",
            },
            "startsAt": "2026-09-27T10:00:00Z",
            "fingerprint": "abc123",
        }
    ],
    "groupLabels": {"alertname": "DiskFull"},
    "commonLabels": {"alertname": "DiskFull", "severity": "critical"},
    "externalURL": "https://alertmanager.example.org",
}


# --------------------------------------------------------------------------- #
# Flattening, which is what makes one renderer work for every service
# --------------------------------------------------------------------------- #


def test_nesting_becomes_dotted_paths() -> None:
    assert flatten({"a": {"b": {"c": 1}}}) == [("a.b.c", 1)]


def test_lists_are_indexed() -> None:
    assert flatten({"a": [{"b": 1}, {"b": 2}]}) == [("a.0.b", 1), ("a.1.b", 2)]


def test_a_bare_scalar_flattens_to_itself() -> None:
    assert flatten("hello") == [("", "hello")]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("startsAt", ["starts", "At"]),
        ("starts_at", ["starts", "at"]),
        ("server-name", ["server", "name"]),
        ("alertname", ["alertname"]),
        ("HTTPStatus", ["HTTPStatus"]),
    ],
)
def test_identifiers_split_whatever_the_house_style(name: str, expected: list[str]) -> None:
    assert words(name) == expected


@pytest.mark.parametrize(
    "path",
    ["id", "_id", "server_id", "ts", "resolved_ts", "startsAt", "updatedAt", "fingerprint"],
)
def test_identifiers_and_timestamps_are_noise(path: str) -> None:
    assert is_noise(path)


@pytest.mark.parametrize("path", ["name", "level", "ruleUrl", "externalURL", "id_provider"])
def test_everything_else_is_kept(path: str) -> None:
    # URLs stay: a link back to the dashboard is usually the useful part.
    assert not is_noise(path)


def test_repeats_of_the_same_name_and_value_collapse() -> None:
    fields = [("a.severity", "critical"), ("b.severity", "critical"), ("c.severity", "warning")]
    assert dedupe(fields) == [("a.severity", "critical"), ("c.severity", "warning")]


# --------------------------------------------------------------------------- #
# The generic renderer
# --------------------------------------------------------------------------- #


def test_komodo_renders_without_any_configuration() -> None:
    text = render(KOMODO)
    assert text.startswith("**CRITICAL**")
    assert "sable" in text
    assert "prod-1" in text
    assert "Running" in text
    assert "Unhealthy" in text
    assert "StackStateChange" in text
    # Identifiers and timestamps are left out.
    assert "66f1a2b3" not in text
    assert "1727441234000" not in text


def test_alertmanager_leads_with_its_summary_and_description() -> None:
    text = render(ALERTMANAGER)
    lines = text.splitlines()
    assert lines[0] == "**CRITICAL** Disk almost full on db01"
    assert lines[1] == "98% of /var used"
    # alertname and severity each appear three times in the payload, once here.
    assert text.count("DiskFull") == 1
    assert "fingerprint" not in text
    assert "startsAt" not in text


def test_a_plain_message_is_left_alone() -> None:
    assert render({"message": "deploy v1.2.3 finished"}) == "deploy v1.2.3 finished"


def test_a_severity_leads_the_headline() -> None:
    text = render({"severity": "warning", "title": "Backup slow"})
    assert text == "**WARNING** Backup slow"


def test_list_indices_keep_their_context() -> None:
    # "0: 1" says nothing on its own.
    text = render({"foo": {"bar": [1, 2]}})
    assert "bar.0: 1" in text
    assert "bar.1: 2" in text


def test_a_long_payload_is_capped_and_says_so() -> None:
    text = render({f"field{i}": f"value{i}" for i in range(20)})
    assert "(+" in text
    assert "more)" in text


def test_a_payload_with_nothing_usable_falls_back_to_json() -> None:
    assert render({}).startswith("```json")


def test_a_bare_string_payload_is_the_message() -> None:
    assert render("something happened") == "something happened"


def test_long_values_are_truncated() -> None:
    text = render({"detail": "x" * 500})
    assert len(text) < 300


# --------------------------------------------------------------------------- #
# Format strings
# --------------------------------------------------------------------------- #


def test_a_template_substitutes_dotted_paths() -> None:
    text, missing = render_with_template(
        "**{level}** {data.type}: {data.data.name} on {data.data.server_name}", KOMODO
    )
    assert text == "**CRITICAL** StackStateChange: sable on prod-1"
    assert missing == []


def test_a_missing_path_becomes_a_question_mark_and_is_reported() -> None:
    text, missing = render_with_template("{level} {nope.missing}", KOMODO)
    assert text == "CRITICAL ?"
    assert missing == ["nope.missing"]


def test_doubled_braces_are_literal() -> None:
    text, _ = render_with_template("literal {{braces}} and {level}", KOMODO)
    assert text == "literal {braces} and CRITICAL"


def test_a_template_can_name_a_whole_subtree() -> None:
    text, missing = render_with_template("{data.data}", KOMODO)
    assert missing == []
    assert '"name": "sable"' in text


def test_a_template_reaches_into_lists() -> None:
    text, _ = render_with_template("{alerts.0.labels.instance}", ALERTMANAGER)
    assert text == "db01"


def test_a_template_with_no_placeholders_is_a_fixed_message() -> None:
    text, missing = render_with_template("something happened", KOMODO)
    assert text == "something happened"
    assert missing == []
