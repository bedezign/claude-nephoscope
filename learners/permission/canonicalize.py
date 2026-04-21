"""Bashlex-driven canonicalization of Bash commands into shape tuples.

The permission learner groups observed commands by ``(verb, subcommand,
sorted-flag-set)``. This module walks a bashlex AST and extracts one
:class:`CanonicalLeaf` per leaf ``CommandNode`` reachable from the top-level
parse — including commands inside pipelines, lists, subshells, command
substitutions, and process substitutions.

Design notes
------------

- Env-var assignments (``FOO=bar cmd ...``) are stripped entirely; the values
  frequently contain secrets (database URLs with credentials, API tokens)
  and must not reach the candidate table.
- Flag *values* are not captured. ``git commit -m "secret message"`` yields
  flag ``-m`` only; the literal message never enters the canonical shape.
- Task runners (``npm run``, ``uv run``, ``make``, ``just``, ``cargo run``,
  ``pdm run``, ``pnpm run``, ``yarn run``) have a special subcommand rule:
  the target name after the runner spec becomes the subcommand, so that
  ``uv run pytest`` canonicalizes to ``(uv, pytest, ...)`` rather than
  ``(uv, run, ...)``.
- ``parse_command`` never raises. On unparseable input it returns ``[]`` and
  the caller treats that as "fall through to the user prompt" — the command
  will not be auto-allowed, but it will not be blocked either.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

import bashlex
import bashlex.ast
import bashlex.errors


# Purely-numeric flag tokens (single dash, one or more digits) collapse to the
# sentinel ``-<N>`` so numeric variants like ``head -40`` and ``head -100``
# canonicalize identically. This keeps numeric arguments distinct from real flag
# tokens that happen to be letters (e.g. ``wget -N``, ``ssh -N``, ``ls -N``).
_NUMERIC_FLAG_RE = re.compile(r"^-\d+$")

# Pure-letter POSIX short-flag cluster: single dash, two or more ASCII letters,
# no `=`, no digits. We split these into per-letter flags so that ``rm -rf`` and
# ``rm -r -f`` canonicalize identically and so per-flag deny patterns can match
# (e.g. ``-f`` inside ``-rf``). Conservative on purpose: we never split tokens
# with letters+digits (``-O3``, ``-j4``), long flags (``--force``), or value-bearing
# forms (``-f=value``). Purely-numeric tokens are handled by ``_NUMERIC_FLAG_RE`` above.
#
# This assumes POSIX cluster convention. It works for ``tar -xvf`` (clustered
# shorts) and ``find`` (mixed, but ``find`` flags mostly don't cluster under a
# single dash). Verbs like ``dd`` use ``if=``/``of=`` which never start with a
# dash, so our flag detector skips them anyway.
_SHORT_FLAG_CLUSTER_RE = re.compile(r"^-[A-Za-z]{2,}$")

# Process/command-substitution literal prefixes we never want to record as a
# subcommand on the *outer* leaf. Recursion into the inner substitution still
# happens in ``_substitutions_in``.
_SUBSTITUTION_PREFIXES: tuple[str, ...] = ("<(", ">(", "$(")


TASK_RUNNERS: frozenset[tuple[str, ...]] = frozenset(
    {
        ("npm", "run"),
        ("pnpm", "run"),
        ("yarn", "run"),
        ("pdm", "run"),
        ("uv", "run"),
        ("cargo", "run"),
        ("make",),
        ("just",),
    }
)

# Verbs whose first positional argument is content (a path, pattern, message,
# file, or script body) rather than a subcommand. For these, ``_resolve_sub-
# command`` returns ``(None, 1)`` so repeated invocations with different
# content collapse into a single canonical shape. Inverse of TASK_RUNNERS:
# task runners promote a positional to subcommand; content verbs discard it.
#
# Destructive potential (``sed -i``, ``find -delete``, ``awk -i inplace``)
# lives in flags, which are still captured — the deny-list can target those
# flag combinations independently.
CONTENT_VERBS: frozenset[str] = frozenset(
    {
        # Display / print
        "echo",
        "printf",
        # Read & filter text
        "cat",
        "head",
        "tail",
        "grep",
        "egrep",
        "fgrep",
        "zgrep",
        "wc",
        "sort",
        "uniq",
        "tr",
        "cut",
        "tac",
        "paste",
        # Script-arg text processors
        "sed",
        "awk",
        # List / query filesystem & processes
        "ls",
        "find",
        "ps",
        "df",
        "du",
        "free",
        "pwd",
        # Inspect files
        "stat",
        "file",
        "readlink",
        "realpath",
        # Resolve names
        "which",
        "type",
        "command",
        "whereis",
        "basename",
        "dirname",
        # System info
        "date",
        "uname",
        "uptime",
        "whoami",
        "hostname",
        "id",
        "groups",
        # File ops — first positional is a path, not a subcommand. Adding these
        # collapses ``rm /a`` / ``rm /b`` / ``rm /c`` into one shape keyed on
        # (rm, None, flags) and lets the scope classifier see all paths.
        "rm",
        "mv",
        "cp",
        "ln",
        "touch",
        "mkdir",
        "rmdir",
        "chmod",
        "chown",
        "chgrp",
    }
)

# Redirections we consider uninteresting for deny-list evaluation.
# "2>&1" has no file target; "> /dev/null" and ">> /dev/null" are harmless.
_NOISE_REDIR_TARGETS: frozenset[str] = frozenset({"/dev/null"})


@dataclass(frozen=True)
class Redirection:
    """A captured redirection on a leaf command.

    ``op`` is the bashlex ``type`` string (``>``, ``>>``, ``<``, ``>&``, ...).
    ``target`` is the literal word the redirection points at, or an empty
    string if the redirection has no word target (e.g. ``2>&1`` uses fd dup).
    """

    op: str
    target: str


@dataclass(frozen=True)
class CanonicalLeaf:
    """One leaf command canonicalized into its pattern shape.

    ``positional_paths`` carries the path-looking positional arguments
    (anything not a flag, not a known subcommand, not a substitution
    expression). The scope module uses these to classify the leaf against
    the session's project root. They are NOT part of the canonical shape
    — shapes stay keyed on (verb, subcommand, flags).
    """

    verb: str
    subcommand: str | None
    flags: frozenset[str]
    redirections: tuple[Redirection, ...]
    raw_leaf: str
    positional_paths: tuple[str, ...] = ()


def parse_command(raw: str) -> list[CanonicalLeaf]:
    """Parse ``raw`` and return one :class:`CanonicalLeaf` per leaf command.

    Never raises. Returns ``[]`` when bashlex refuses the input or the top
    level is empty.
    """
    if not raw or not raw.strip():
        return []
    try:
        trees = bashlex.parse(raw)
    except bashlex.errors.ParsingError:
        return []
    except (NotImplementedError, AttributeError, IndexError, ValueError):
        # bashlex occasionally raises other exception types on weird input;
        # swallow them so the hook never breaks the user's tool call.
        return []

    leaves: list[CanonicalLeaf] = []
    for tree in trees:
        _walk(tree, leaves)
    return leaves


# ---------------------------------------------------------------------------
# AST walking
# ---------------------------------------------------------------------------


def _walk(node: bashlex.ast.node, out: list[CanonicalLeaf]) -> None:
    """Recursively collect leaf commands from ``node``."""
    kind = getattr(node, "kind", None)
    if kind == "command":
        _process_command(node, out)
        return
    # Compound nodes: recurse into `parts` (pipelines, lists, compound, ...).
    for child in getattr(node, "parts", ()) or ():
        _walk(child, out)


def _process_command(node: bashlex.ast.node, out: list[CanonicalLeaf]) -> None:
    """Extract a CanonicalLeaf from a ``CommandNode`` and recurse into any
    command/process substitutions reachable from its words."""
    parts = list(getattr(node, "parts", ()) or ())
    words: list[bashlex.ast.node] = []
    redirects: list[bashlex.ast.node] = []
    seen_non_assignment = False

    for part in parts:
        pkind = getattr(part, "kind", None)
        if pkind == "assignment" and not seen_non_assignment:
            # Drop env-var assignments entirely; never keep values.
            continue
        if pkind == "word":
            seen_non_assignment = True
            words.append(part)
            # Recurse into command/process substitutions nested in the word.
            for sub in _substitutions_in(part):
                _walk(sub, out)
            continue
        if pkind == "redirect":
            redirects.append(part)
            # Redirection targets can contain substitutions too.
            target = getattr(part, "output", None)
            if target is not None and getattr(target, "kind", None) == "word":
                for sub in _substitutions_in(target):
                    _walk(sub, out)
            continue
        # Anything else (operators etc.) we ignore at this level.

    if not words:
        return

    verb = _word_literal(words[0])
    if not verb:
        return

    subcommand, positional_start = _resolve_subcommand(verb, words)
    flags = _collect_flags(words[positional_start:])
    positional_paths = _collect_positional_paths(words[positional_start:])
    redirections = tuple(_canonical_redirections(redirects))
    raw_leaf = _raw_leaf(node)

    out.append(
        CanonicalLeaf(
            verb=verb,
            subcommand=subcommand,
            flags=flags,
            redirections=redirections,
            raw_leaf=raw_leaf,
            positional_paths=positional_paths,
        )
    )


def _substitutions_in(word_node: bashlex.ast.node) -> Iterable[bashlex.ast.node]:
    """Yield command/process substitution nodes referenced by a word."""
    for part in getattr(word_node, "parts", ()) or ():
        pkind = getattr(part, "kind", None)
        if pkind in ("commandsubstitution", "processsubstitution"):
            cmd = getattr(part, "command", None)
            if cmd is not None:
                yield cmd


def _word_literal(word_node: bashlex.ast.node) -> str:
    """The literal text of a WordNode as bashlex parsed it."""
    value = getattr(word_node, "word", None)
    return value if isinstance(value, str) else ""


def _resolve_subcommand(
    verb: str, words: list[bashlex.ast.node]
) -> tuple[str | None, int]:
    """Pick a subcommand and report how many positional words to skip.

    Returns ``(subcommand, positional_start_index)`` where
    ``positional_start_index`` is the index in ``words`` at which flag
    collection should begin (i.e. past the verb and any consumed runner
    prefix / subcommand slot).
    """
    # Task-runner check: ("verb", "second_word") or ("verb",) in TASK_RUNNERS.
    if len(words) >= 2:
        second = _word_literal(words[1])
        if (verb, second) in TASK_RUNNERS and len(words) >= 3:
            target = _word_literal(words[2])
            if (
                target
                and not target.startswith("-")
                and not target.startswith(_SUBSTITUTION_PREFIXES)
            ):
                return target, 3
            return None, 2
    if (verb,) in TASK_RUNNERS and len(words) >= 2:
        target = _word_literal(words[1])
        if (
            target
            and not target.startswith("-")
            and not target.startswith(_SUBSTITUTION_PREFIXES)
        ):
            return target, 2
        return None, 1

    # Content verbs: first positional is content (path/pattern/message/script),
    # not a subcommand. Collapse all invocations into one shape by discarding
    # the positional slot entirely.
    if verb in CONTENT_VERBS:
        return None, 1

    # Default: first non-flag token after the verb.
    if len(words) >= 2:
        candidate = _word_literal(words[1])
        if candidate and not candidate.startswith("-"):
            # Process/command substitutions (``<(...)``, ``>(...)``, ``$(...)``)
            # are not meaningful subcommand names — drop the slot. Recursion
            # into the inner command still happens via _substitutions_in.
            if candidate.startswith(_SUBSTITUTION_PREFIXES):
                return None, 2
            return candidate, 2
    return None, 1


def _collect_flags(words: Iterable[bashlex.ast.node]) -> frozenset[str]:
    """Return the sorted, deduped set of flag tokens in ``words``.

    Values for flags are dropped — each dash-prefixed token contributes
    itself and nothing more. (``--verbose`` and ``-v`` are both captured;
    ``-m "message"`` yields only ``-m``.)

    POSIX short-flag clusters (``-rf``, ``-la``, ``-xvf``) are split into
    per-letter flags so ``rm -rf`` and ``rm -r -f`` produce the same shape
    and per-flag deny patterns can match clustered forms.

    Purely-numeric flag tokens (``-10``, ``-40``, ``-100``) collapse to the
    sentinel ``-<N>`` so numeric variants canonicalize identically.
    """
    out: set[str] = set()
    for word in words:
        literal = _word_literal(word)
        if not (literal.startswith("-") and len(literal) > 1):
            continue
        # A real flag never contains whitespace. A word that starts with `-`
        # and has an internal space is quoted content (``"--- banner ---"``),
        # not a flag — common now that CONTENT_VERBS routes first positionals
        # through flag collection.
        if any(ch.isspace() for ch in literal):
            continue
        # Split on "=" so "--file=foo" is captured as "--file". A token with
        # "=" is treated as value-bearing and never cluster-split.
        if "=" in literal:
            out.add(literal.split("=", 1)[0])
            continue
        if _NUMERIC_FLAG_RE.match(literal):
            out.add("-<N>")
            continue
        if _SHORT_FLAG_CLUSTER_RE.match(literal):
            # Pure-letter POSIX cluster: split each letter into its own flag.
            for ch in literal[1:]:
                out.add(f"-{ch}")
            continue
        out.add(literal)
    return frozenset(out)


def _collect_positional_paths(words: Iterable[bashlex.ast.node]) -> tuple[str, ...]:
    """Return positional args that look like paths — for scope classification.

    Skips flags, flag-values-that-happen-to-be-adjacent (we don't know which
    flags take values without a command database), substitution expressions
    (``$(...)``, ``<(...)``, ``>(...)``), and empty strings. The output is
    the set of literal tokens the caller will feed into ``classify_paths``.

    Conservative: any token that isn't clearly a flag or substitution ends
    up here, including URLs, hostnames, and non-filesystem args. That's OK
    — ``classify_paths`` resolves each to an absolute filesystem path; a
    URL resolves outside the project root and contributes to ``mixed`` /
    ``outside_project``, which is the safe default.
    """
    out: list[str] = []
    for word in words:
        literal = _word_literal(word)
        if not literal:
            continue
        if literal.startswith("-"):
            continue
        if literal.startswith(_SUBSTITUTION_PREFIXES):
            continue
        out.append(literal)
    return tuple(out)


def _canonical_redirections(redirects: list[bashlex.ast.node]) -> list[Redirection]:
    """Map RedirectNodes to our Redirection dataclass, dropping noise."""
    out: list[Redirection] = []
    for node in redirects:
        op = getattr(node, "type", "") or ""
        target_node = getattr(node, "output", None)
        if target_node is None or getattr(target_node, "kind", None) != "word":
            # fd-only redirections like 2>&1 have no word target; skip them.
            continue
        target = _word_literal(target_node)
        if not target:
            continue
        if target in _NOISE_REDIR_TARGETS:
            continue
        out.append(Redirection(op=op, target=target))
    return out


def _raw_leaf(node: bashlex.ast.node) -> str:
    """Best-effort reconstruction of the command text for the leaf."""
    parts = getattr(node, "parts", None)
    if not parts:
        return ""
    pieces: list[str] = []
    for part in parts:
        pkind = getattr(part, "kind", None)
        if pkind == "word":
            pieces.append(_word_literal(part))
    return " ".join(p for p in pieces if p)
