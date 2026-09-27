"""Turning somebody else's webhook payload into a chat message.

Services that want to post alerts each have their own JSON shape, and most of
them cannot be persuaded to send sable's. Rather than a handler per service, or
a template language to configure, this renders any JSON generically:

1. flatten the payload to dotted paths with scalar leaves, so nesting stops
   mattering;
2. pick out a severity, a title and a body by looking for the names services
   actually use;
3. show whatever is left as key/value pairs, dropping the parts nobody wants to
   read in a chat room.

The flattening is what makes one renderer work for Komodo, Alertmanager, Grafana
and whatever turns up next: the interesting fields are rarely at the top level,
but they are almost always scalars somewhere.

A hook can also be given a format string, in which case `{dotted.path}` is
substituted from the same flattened payload and the guessing is skipped
entirely. Substitution only: no expressions, no logic, nothing that can run.
"""

from __future__ import annotations

import json
import re
from typing import Any

#: Leaf names that carry how bad it is, in the order we prefer them.
SEVERITY_KEYS = ("level", "severity", "status", "state", "priority", "urgency")

#: Leaf names that make a good headline.
TITLE_KEYS = ("title", "subject", "summary", "alertname", "event", "name", "type")

#: Leaf names that carry the prose.
BODY_KEYS = ("message", "text", "description", "details", "body", "reason", "error", "err")

#: Dropped before rendering. Identifiers and timestamps are noise in a chat
#: message, which already has its own time and its own context. URLs are kept:
#: a link back to the dashboard that fired is usually the most useful part.
NOISE_WORDS = frozenset(
    {"id", "uuid", "guid", "fingerprint", "ts", "timestamp", "time", "date", "at"}
)

MAX_FIELDS = 8
MAX_VALUE_CHARS = 200
MAX_JSON_CHARS = 1500


def flatten(value: Any, prefix: str = "") -> list[tuple[str, Any]]:
    """Every scalar leaf, as (dotted path, value), in document order."""
    if isinstance(value, dict):
        out: list[tuple[str, Any]] = []
        for key, item in value.items():
            out.extend(flatten(item, f"{prefix}.{key}" if prefix else str(key)))
        return out
    if isinstance(value, list):
        out = []
        for index, item in enumerate(value):
            out.extend(flatten(item, f"{prefix}.{index}" if prefix else str(index)))
        return out
    return [(prefix, value)]


def leaf(path: str) -> str:
    return path.rsplit(".", 1)[-1]


def words(name: str) -> list[str]:
    """Split an identifier into words, whatever convention it was written in.

    ``startsAt``, ``starts_at`` and ``starts-at`` all come back as
    ``["starts", "at"]``, so one rule covers every service's house style.
    """
    return [w for w in re.split(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])", name) if w]


def is_noise(path: str) -> bool:
    parts = words(leaf(path))
    if not parts:
        return False
    # The last word decides: `server_id` and `startsAt` go, `id_provider` stays.
    return parts[-1].lower() in NOISE_WORDS


def is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value).strip()
    if len(text) > MAX_VALUE_CHARS:
        text = text[: MAX_VALUE_CHARS - 1].rstrip() + "…"
    return text


def take(
    fields: list[tuple[str, Any]], names: tuple[str, ...]
) -> tuple[str | None, str | None]:
    """Find the first field whose leaf name is one of ``names``.

    Returns the path and its value, and leaves the list untouched - the caller
    removes what it used, so one field cannot be both the title and the body.
    """
    for wanted in names:
        for path, value in fields:
            if leaf(path).lower() == wanted and not is_empty(value):
                text = scalar(value)
                # A paragraph is a body, not a headline.
                if wanted in TITLE_KEYS and ("\n" in text or len(text) > 120):
                    continue
                return path, text
    return None, None


def short(path: str) -> str:
    """A readable label for a path, never a bare list index."""
    name = leaf(path)
    if name.isdigit():
        # "foo.bar.0" reads as nothing on its own; "bar.0" says where it came from.
        return ".".join(path.split(".")[-2:])
    return name


def labels(paths: list[str]) -> dict[str, str]:
    """Shortest unambiguous label per path: the short form unless it collides."""
    counts: dict[str, int] = {}
    for path in paths:
        counts[short(path)] = counts.get(short(path), 0) + 1
    return {path: (short(path) if counts[short(path)] == 1 else path) for path in paths}


def dedupe(fields: list[tuple[str, Any]]) -> list[tuple[str, Any]]:
    """Drop repeats of the same name and value.

    Alert payloads say the same thing several times over - Alertmanager repeats
    `alertname` and `severity` in the alert, the group labels and the common
    labels - and a chat message only needs it once.
    """
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, Any]] = []
    for path, value in fields:
        key = (leaf(path).lower(), scalar(value))
        if key in seen:
            continue
        seen.add(key)
        out.append((path, value))
    return out


def render(payload: Any) -> str:
    """Render any JSON payload as a Markdown chat message."""
    if not isinstance(payload, (dict, list)):
        return scalar(payload) if not is_empty(payload) else "(empty webhook payload)"

    fields = [
        (path, value)
        for path, value in flatten(payload)
        if not is_empty(value) and not is_noise(path)
    ]
    if not fields:
        return _as_json(payload)

    used: set[str] = set()

    severity_path, severity = take(fields, SEVERITY_KEYS)
    if severity_path:
        used.add(severity_path)

    title_path, title = take(
        [f for f in fields if f[0] not in used], TITLE_KEYS
    )
    if title_path:
        used.add(title_path)

    body_path, body = take([f for f in fields if f[0] not in used], BODY_KEYS)
    if body_path:
        used.add(body_path)

    rest = dedupe([(path, value) for path, value in fields if path not in used])

    headline = ""
    if severity:
        headline = f"**{severity.upper()}**" if len(severity) <= 20 else f"**{severity}**"
    if title:
        headline = f"{headline} {title}".strip()

    lines: list[str] = []
    if headline:
        lines.append(headline)
    if body:
        lines.append(body)

    if rest:
        shown, extra = rest[:MAX_FIELDS], len(rest) - MAX_FIELDS
        label = labels([path for path, _ in shown])
        pairs = " · ".join(f"{label[path]}: {scalar(value)}" for path, value in shown)
        if extra > 0:
            pairs += f" · (+{extra} more)"
        lines.append(pairs)

    if not lines:
        return _as_json(payload)
    return "\n".join(lines)


#: `{dotted.path}` in a hook's format string. `{{` and `}}` are literal braces.
PLACEHOLDER = re.compile(r"\{([^{}]*)\}")

_OPEN, _CLOSE = "\x00", "\x01"


def lookup(payload: Any) -> dict[str, str]:
    """Every path a format string may reference, as text.

    Leaves come from :func:`flatten`. Containers are included too, rendered as
    compact JSON, so `{data}` gives you something rather than a question mark.
    """
    values = {path: scalar(value) for path, value in flatten(payload) if not is_empty(value)}

    def walk(node: Any, prefix: str) -> None:
        if isinstance(node, dict):
            items: Any = node.items()
        elif isinstance(node, list):
            items = enumerate(node)
        else:
            return
        if prefix:
            values.setdefault(prefix, json.dumps(node, ensure_ascii=False, default=str))
        for key, item in items:
            walk(item, f"{prefix}.{key}" if prefix else str(key))

    walk(payload, "")
    return values


def render_with_template(template: str, payload: Any) -> tuple[str, list[str]]:
    """Substitute `{dotted.path}` from the payload.

    Returns the text and the paths that were not found, which the caller logs.
    A missing path becomes `?` rather than an error: an alert that arrives
    slightly wrong beats an alert that does not arrive.
    """
    values = lookup(payload)
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        path = match.group(1).strip()
        if path in values:
            return values[path]
        missing.append(path)
        return "?"

    escaped = template.replace("{{", _OPEN).replace("}}", _CLOSE)
    text = PLACEHOLDER.sub(replace, escaped)
    return text.replace(_OPEN, "{").replace(_CLOSE, "}").strip(), missing


def _as_json(payload: Any) -> str:
    """Last resort: show the payload itself rather than nothing."""
    text = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    if len(text) > MAX_JSON_CHARS:
        text = text[:MAX_JSON_CHARS] + "\n…"
    return f"```json\n{text}\n```"
