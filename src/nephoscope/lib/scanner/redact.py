"""Output-scanner redactor.

Replaces matched secrets in scanned text with ``[REDACTED:<pattern_name>]``
markers, or merely records matches in warn mode.
"""

from __future__ import annotations

from dataclasses import dataclass

from nephoscope.lib.scanner.patterns import CompiledPattern

_VALID_MODES = frozenset({"redact", "warn"})


@dataclass(frozen=True)
class MatchRecord:
    """One non-overlapping match found by the redactor.

    Attributes:
        name: The name of the pattern that matched.
        start: Character offset of the match start in the original text.
        end: Character offset just past the match end in the original text.
    """

    name: str
    start: int
    end: int


@dataclass(frozen=True)
class RedactResult:
    """Outcome of a redact() call.

    Attributes:
        text: Redacted text in ``redact`` mode, or the original input in
            ``warn`` mode.
        matches: One :class:`MatchRecord` per non-overlapping match found,
            regardless of mode.
    """

    text: str
    matches: list[MatchRecord]


def redact(
    text: str,
    patterns: list[CompiledPattern],
    *,
    mode: str = "redact",
) -> RedactResult:
    """Redact (or warn on) secrets matched by ``patterns`` in ``text``.

    Matches from all patterns are collected, then overlaps are resolved
    deterministically: earlier-starting matches win, ties broken by longer
    span, then by pattern order in the input list. Surviving matches are
    used both for the ``matches`` list and (in redact mode) for substitution.

    Args:
        text: The input text to scan.
        patterns: Compiled patterns to match against the text.
        mode: ``'redact'`` to replace matches with ``[REDACTED:<name>]``
            markers, ``'warn'`` to leave text unchanged but still record
            matches.

    Returns:
        A :class:`RedactResult` with the (possibly redacted) text and the
        list of non-overlapping matches recorded.

    Raises:
        ValueError: If ``mode`` is not one of ``'redact'`` or ``'warn'``.
    """
    if mode not in _VALID_MODES:
        raise ValueError(f"mode must be one of {sorted(_VALID_MODES)}, got {mode!r}")

    # Collect raw spans across all patterns.
    raw: list[tuple[int, int, int, str]] = []
    for pattern_index, compiled in enumerate(patterns):
        for match in compiled.regex.finditer(text):
            start, end = match.span()
            if start == end:
                # Skip zero-width matches: they cannot be redacted meaningfully
                # and would loop forever in any naive substitution scheme.
                continue
            raw.append((start, end, pattern_index, compiled.name))

    # Sort by: earliest start, then longest span, then pattern declaration order.
    raw.sort(key=lambda item: (item[0], -(item[1] - item[0]), item[2]))

    # Sweep, keeping non-overlapping spans only.
    selected: list[MatchRecord] = []
    last_end = 0
    for start, end, _, name in raw:
        if start < last_end:
            continue
        selected.append(MatchRecord(name=name, start=start, end=end))
        last_end = end

    if mode == "warn":
        return RedactResult(text=text, matches=selected)

    # Build redacted text by walking surviving spans in order.
    parts: list[str] = []
    cursor = 0
    for record in selected:
        parts.append(text[cursor : record.start])
        parts.append(f"[REDACTED:{record.name}]")
        cursor = record.end
    parts.append(text[cursor:])
    return RedactResult(text="".join(parts), matches=selected)
