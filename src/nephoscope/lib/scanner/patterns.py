"""Output-scanner pattern loader.

Loads a YAML file shaped as ``{patterns: [{name: str, pattern: str}, ...]}``
into a list of :class:`CompiledPattern` value objects with pre-compiled regexes.

The ``path`` argument is duck-typed on ``read_text()`` so both
:class:`pathlib.Path` and :class:`importlib.resources.abc.Traversable` work
without conversion.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import yaml


@dataclass(frozen=True)
class CompiledPattern:
    name: str
    regex: re.Pattern


def load_patterns(path: Any) -> list[CompiledPattern]:
    """Load and compile output-scanner patterns from a YAML file.

    Args:
        path: A path-like object exposing ``read_text()`` — either a
            :class:`pathlib.Path` or an :class:`importlib.resources.abc.Traversable`.

    Returns:
        A list of :class:`CompiledPattern` instances, one per YAML entry.

    Raises:
        ValueError: If any entry is missing the ``name`` or ``pattern`` key,
            or if any ``pattern`` value is not a valid regular expression.
    """
    text = path.read_text()
    data = yaml.safe_load(text)

    if not isinstance(data, dict):
        raise ValueError(
            f"expected top-level mapping in patterns YAML, got {type(data).__name__}"
        )

    entries = data.get("patterns", [])
    if not isinstance(entries, list):
        raise ValueError(
            f"expected 'patterns' to be a list, got {type(entries).__name__}"
        )

    compiled: list[CompiledPattern] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(
                f"pattern entry at index {index} is not a mapping: {entry!r}"
            )

        if "name" not in entry:
            raise ValueError(
                f"pattern entry at index {index} is missing required key 'name'"
            )
        if "pattern" not in entry:
            raise ValueError(
                f"pattern entry {entry['name']!r} is missing required key 'pattern'"
            )

        name = entry["name"]
        pattern_src = entry["pattern"]

        try:
            regex = re.compile(pattern_src)
        except re.error as exc:
            raise ValueError(
                f"pattern {name!r} has invalid regex {pattern_src!r}: {exc}"
            ) from exc

        compiled.append(CompiledPattern(name=name, regex=regex))

    return compiled
