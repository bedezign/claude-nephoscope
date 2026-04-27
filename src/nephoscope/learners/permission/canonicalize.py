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

Pattern variants
----------------

:func:`to_pattern_form` generalizes a :class:`CanonicalLeaf` into a list of
:class:`PatternVariant` objects — one per candidate ``rule_shapes`` key.
Each variant is a ``(verb, subcommand, flags, path_spec)`` tuple suitable for
a DB lookup or for the per-axis review prompts in ``review.sh``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
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


# Verbs whose CLI groups commands as ``<verb> <group> <action>`` rather than
# ``<verb> <subcommand>``. For these, the second token is a subgroup (not a
# final subcommand) and ``_resolve_subcommand`` should join ``"<group> <action>"``
# into a single space-separated subcommand. The third token must be a real
# positional (not a flag, not a substitution) — otherwise we fall through to
# the default single-word resolution and the leaf gets ``subcommand=<group>``.
#
# Reserved for genuine multi-word CLIs only — adding entries here changes how
# every shape with that verb is canonicalized, so seed rules and stored
# candidates have to agree on the form. ``aws``, ``gcloud``, ``kubectl``, ``gh``
# are likely future entries but are NOT added here without the matching seed
# rules to back them.
TWO_WORD_SUBCOMMAND_VERBS: frozenset[tuple[str, str]] = frozenset(
    {
        ("vault", "kv"),
        ("vault", "auth"),
        ("vault", "secrets"),
        ("doppler", "secrets"),
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

# Mapping from ctx dict key to the $VAR sentinel used in rule_shapes.
_CTX_VAR_NAMES: dict[str, str] = {
    "project_root": "$PROJECT_ROOT",
    "cwd": "$CWD",
    "home": "$HOME",
}

# Priority order for ctx vars — lower number = checked first (most specific).
_CTX_PRIORITY: dict[str, int] = {
    "project_root": 0,
    "cwd": 1,
    "home": 2,
}


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

    ``is_substitution_child`` is True when this leaf was reached by
    recursing into a commandsubstitution or processsubstitution node
    (i.e. the command lives inside ``$(...)`` or ``<(...)``). It defaults
    to False so all existing callers compile without change.
    """

    verb: str
    subcommand: str | None
    flags: frozenset[str]
    redirections: tuple[Redirection, ...]
    raw_leaf: str
    positional_paths: tuple[str, ...] = ()
    is_substitution_child: bool = False


@dataclass(frozen=True)
class PatternVariant:
    """One candidate rule-shape key for DB lookup or per-axis review prompts.

    Fields map directly onto ``rule_shapes`` columns:

    ``verb``
        Literal command name, or ``"$VAR/rest"`` when the verb is an absolute
        path under a recognised context variable (project root, cwd, home).

    ``subcommand``
        Carried through unchanged from the leaf.

    ``flags``
        Minified JSON array of sorted flag strings (e.g. ``'["-q","--verbose"]'``),
        or the sentinel ``"*"`` for the flags-wildcard variant.

    ``path_spec``
        ``None``  — any paths (no path constraint).
        ``""``    — leaf had no positional paths.
        ``"$VAR/**"``    — any path under the named context variable.
        ``"$VAR/<tail>"``— specific subpath relative to the context variable.

    ``context``
        The actual invocation context of the leaf this variant was derived
        from. Values: ``"toplevel"`` (command is at the top of the shell
        command tree) or ``"substitution"`` (command is inside a
        ``$(...)`` or ``<(...)``).  Derived from
        :attr:`CanonicalLeaf.is_substitution_child` in
        :func:`to_pattern_form`.

        The match SQL binds this value against the ``rule_shapes.context``
        column (which stores the *rule's* constraint: ``"any"``,
        ``"toplevel"``, or ``"substitution"``).  A rule with
        ``context='any'`` matches every leaf; a rule with
        ``context='toplevel'`` only matches top-level leaves.

        Note on wildcard-verb (``verb="*"``)/context interaction: a
        substitution-child leaf emits ``context='substitution'`` on all its
        variants, including the wildcard-verb one.  A credential-path seed
        rule with ``context='any'`` therefore still fires on substitution
        children of those paths — which is correct behaviour (reading
        ``~/.aws/credentials`` inside a substitution is just as dangerous).
        Only ``context='toplevel'`` rules are specifically scoped to avoid
        substitution children.

    Review scripts can detect the variant type without extra metadata:
        - ``verb.startswith("$")``          → verb has a $VAR pattern
        - ``path_spec and "$" in path_spec`` → path_spec has a $VAR pattern
        - ``flags == "*"``                   → flags wildcard
        - ``context == "substitution"``      → leaf is inside $(...) or <(...)
    """

    verb: str
    subcommand: str | None
    flags: str
    path_spec: str | None
    context: str = "toplevel"


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


def to_pattern_form(
    leaf: CanonicalLeaf,
    ctx: dict[str, str],
    additional_dirs: list[str] | None = None,
) -> list[PatternVariant]:
    """Return pattern variants for ``leaf`` given path context.

    ``ctx`` accepts any subset of ``{"cwd", "project_root", "home"}`` mapping
    to absolute directory paths for the current session.

    ``additional_dirs`` is an optional list of absolute directory paths from
    ``permissions.additionalDirectories`` (global + project merged).  Positional
    paths that fall under one of these directories and do not match any ctx-var
    prefix are emitted as **inline absolute** path-specs (e.g.
    ``"/opt/company/shared/**"`` and ``"/opt/company/shared/build/output"``).
    No ``$VAR`` placeholder is introduced — the matcher's existing
    ``"$" in path_spec`` branch already bypasses placeholder resolution for
    literal absolute specs, so they round-trip cleanly through the DB.

    Returns a list of :class:`PatternVariant` objects covering:

    1. **Literal** — verb and flags as-is; path_spec derived from whether the
       leaf has any positional paths.
    2. **Verb-patterned** — if the verb is an absolute path under a ctx var,
       one variant with the matching prefix replaced by ``$VAR``.  Longest
       prefix wins; PROJECT_ROOT beats CWD beats HOME on equal length.
    3. **Path-spec variants** — one ``$VAR/**`` and one ``$VAR/<tail>`` variant
       per distinct positional path that lies under a ctx var.  Both use the
       verb-patterned form when available.  For paths that fall under an
       additional_dir instead, inline absolute path-specs are emitted.
    4. **Flags-wildcard** — a single variant with ``flags="*"`` and the best
       available verb form.

    Duplicates are suppressed (set-tracked) but insertion order is preserved
    so the most specific variants come first.
    """
    ordered = _ctx_pairs_ordered(ctx)
    norm_extra = _normalise_dirs(additional_dirs)
    flags_literal = _flags_json(leaf.flags)

    verb_patterned = _substitute_prefix(leaf.verb, ordered)
    best_verb = verb_patterned if verb_patterned is not None else leaf.verb
    sub = leaf.subcommand

    # Derive the actual context from the leaf's substitution status.
    # "toplevel"    — command was at the top level of the shell command tree.
    # "substitution"— command was inside a $(...) or <(...).
    leaf_context = "substitution" if leaf.is_substitution_child else "toplevel"

    # base path_spec: "" when no positionals at all; None otherwise (any)
    base_path_spec: str | None = "" if not leaf.positional_paths else None

    # Collect distinct path_spec strings derived from positional_paths.
    # Pass norm_extra (already normalised) so _path_specs_from_positionals
    # does not re-run _normalise_dirs a second time.
    path_specs: list[str] = _path_specs_from_positionals(
        leaf.positional_paths, ordered, norm_extra
    )

    seen: set[PatternVariant] = set()
    variants: list[PatternVariant] = []

    def _add(v: PatternVariant) -> None:
        if v not in seen:
            seen.add(v)
            variants.append(v)

    # 1. Literal base.
    _add(
        PatternVariant(
            verb=leaf.verb,
            subcommand=sub,
            flags=flags_literal,
            path_spec=base_path_spec,
            context=leaf_context,
        )
    )

    # 2. Verb-patterned (only when different from literal).
    if verb_patterned is not None and verb_patterned != leaf.verb:
        _add(
            PatternVariant(
                verb=verb_patterned,
                subcommand=sub,
                flags=flags_literal,
                path_spec=base_path_spec,
                context=leaf_context,
            )
        )

    # 3. Path-spec variants — use the best verb form.
    for ps in path_specs:
        _add(
            PatternVariant(
                verb=best_verb,
                subcommand=sub,
                flags=flags_literal,
                path_spec=ps,
                context=leaf_context,
            )
        )

    # 4. Flags-wildcard — use the best verb form.
    _add(
        PatternVariant(
            verb=best_verb,
            subcommand=sub,
            flags="*",
            path_spec=base_path_spec,
            context=leaf_context,
        )
    )

    # 5. Wildcard-verb — one per distinct path_spec derived from positionals.
    # verb="*" covers every reader verb on a credential path so a single seed
    # rule blocks cat, grep, head, etc. without N per-verb rows.
    # These come AFTER per-verb path-spec variants (step 3) so per-verb rules
    # win on lookup when both exist for the same path.
    for ps in path_specs:
        _add(
            PatternVariant(
                verb="*",
                subcommand=None,
                flags="*",
                path_spec=ps,
                context=leaf_context,
            )
        )

    return variants


# ---------------------------------------------------------------------------
# Pattern-variant helpers
# ---------------------------------------------------------------------------


def _ctx_pairs_ordered(ctx: dict[str, str]) -> list[tuple[str, str]]:
    """Return ``("$VAR", "/path")`` pairs sorted longest path first.

    On equal path length, PROJECT_ROOT beats CWD beats HOME.
    """
    pairs: list[tuple[str, str]] = []
    for key, var_name in _CTX_VAR_NAMES.items():
        path = ctx.get(key, "").rstrip("/")
        if path:
            pairs.append((var_name, path))
    pairs.sort(
        key=lambda p: (
            -len(p[1]),
            _CTX_PRIORITY.get({v: k for k, v in _CTX_VAR_NAMES.items()}[p[0]], 99),
        )
    )
    return pairs


def _substitute_prefix(s: str, ordered: list[tuple[str, str]]) -> str | None:
    """Replace the longest matching ctx prefix in ``s`` with its ``$VAR`` name.

    Returns ``None`` when no ctx prefix matches, so the caller can distinguish
    "no substitution available" from a substitution that produced the same string.
    Only applies when ``s`` is an absolute path (starts with ``/``).
    """
    if not s.startswith("/"):
        return None
    for var_name, base in ordered:
        if s == base:
            return var_name
        if s.startswith(base + "/"):
            return var_name + s[len(base) :]
    return None


def _normalise_dirs(dirs: list[str] | None) -> list[str]:
    """Expand ``~``, strip trailing slashes, skip blank entries.

    Returns an empty list when ``dirs`` is None or empty so callers can
    iterate unconditionally.  Non-existent directories are kept as-is — the
    match is purely lexicographic, not filesystem-based, so a directory that
    doesn't exist on disk is tolerated without error.
    """
    if not dirs:
        return []
    return [str(Path(d).expanduser()).rstrip("/") for d in dirs if d and d.strip()]


def _emit_path_spec(
    glob: str,
    specific: str | None,
    seen: set[str],
    result: list[str],
) -> None:
    """Append glob (and optionally specific) to result if not already seen."""
    if glob not in seen:
        seen.add(glob)
        result.append(glob)
    if specific is not None and specific not in seen:
        seen.add(specific)
        result.append(specific)


def _match_ctx_prefix(
    path: str,
    ordered: list[tuple[str, str]],
    seen: set[str],
    result: list[str],
) -> bool:
    """Emit variants under every matching ctx-var prefix; return whether any matched.

    Real-world sessions commonly have overlapping ctx vars — ``PROJECT_ROOT``
    and ``CWD`` are equal in the typical "session opened at the project root"
    case. Emitting under only the longest-prefix winner means seed rules keyed
    on a different ctx-var (e.g. ``$CWD/**/.env``) never match. The variant
    emitter's job is to enumerate every reasonable rule key for the leaf; the
    DB lookup decides which key actually has a rule. ``seen`` still dedupes
    identical strings.
    """
    matched = False
    for var_name, base in ordered:
        glob = var_name + "/**"
        if path == base:
            _emit_path_spec(glob, None, seen, result)
            matched = True
            continue
        if path.startswith(base + "/"):
            tail = path[len(base) + 1 :]
            specific = var_name + "/" + tail
            _emit_path_spec(glob, specific, seen, result)
            # Basename-glob: matches any depth ending in this filename.
            basename = tail.rsplit("/", 1)[-1] if "/" in tail else tail
            if basename:
                basename_glob = var_name + "/**/" + basename
                _emit_path_spec(basename_glob, None, seen, result)
            matched = True
    return matched


def _match_additional_dir(
    path: str,
    norm_extra: list[str],
    seen: set[str],
    result: list[str],
) -> None:
    """Try matching path against additional_dirs; emit inline absolute specs."""
    for base in norm_extra:
        glob = base + "/**"
        if path == base:
            _emit_path_spec(glob, None, seen, result)
            break
        if path.startswith(base + "/"):
            tail = path[len(base) + 1 :]
            specific = base + "/" + tail
            _emit_path_spec(glob, specific, seen, result)
            break


def _path_specs_from_positionals(
    positional_paths: tuple[str, ...],
    ordered: list[tuple[str, str]],
    additional_dirs: list[str] | None = None,
) -> list[str]:
    """Derive ``path_spec`` strings from positional path arguments.

    For each positional path that lies under a ctx variable, emit:
      - ``"$VAR/**"``        — any path under that variable
      - ``"$VAR/<tail>"``    — the specific subpath

    For each positional path that lies under an additional_dir (and did NOT
    match any ctx variable), emit the inline absolute equivalents:
      - ``"<dir>/**"``       — any path under the additional directory
      - ``"<dir>/<tail>"``   — the specific subpath

    Ctx-var matches take priority over additional_dir matches — a path under
    both ``$PROJECT_ROOT`` and an additional_dir will be emitted with the
    ``$PROJECT_ROOT`` placeholder, not the inline form.

    Deduplicates while preserving insertion order (``**`` glob before specific
    path for the same base, ordered by the position of the matched directory).

    ``additional_dirs`` may be pre-normalised (from ``to_pattern_form``) or raw.
    ``_normalise_dirs`` is idempotent on already-absolute paths, so calling it
    here is safe either way and ensures direct callers also get expansion.
    """
    norm_extra = _normalise_dirs(additional_dirs)
    seen: set[str] = set()
    result: list[str] = []
    cwd = next((base for var, base in ordered if var == "$CWD"), None)

    for path in positional_paths:
        if not path.startswith("/"):
            if cwd is None:
                continue
            path = cwd + "/" + path
        if _match_ctx_prefix(path, ordered, seen, result):
            continue
        _match_additional_dir(path, norm_extra, seen, result)

    return result


def _flags_json(flags: frozenset[str]) -> str:
    """Minified JSON array of sorted flag strings."""
    return json.dumps(sorted(flags), separators=(",", ":"))


# ---------------------------------------------------------------------------
# AST walking
# ---------------------------------------------------------------------------


def _walk(
    node: bashlex.ast.node,
    out: list[CanonicalLeaf],
    *,
    in_substitution: bool = False,
) -> None:
    """Recursively collect leaf commands from ``node``.

    ``in_substitution`` is propagated down from ``_handle_word_part`` and
    ``_handle_redirect_part`` when recursing into commandsubstitution or
    processsubstitution nodes.  It is flipped to ``True`` before recursing
    into the inner command, so any :class:`CanonicalLeaf` produced there
    carries :attr:`~CanonicalLeaf.is_substitution_child` = True.
    """
    kind = getattr(node, "kind", None)
    if kind == "command":
        _process_command(node, out, in_substitution=in_substitution)
        return
    # Compound nodes: recurse into `parts` (pipelines, lists, compound, ...).
    for child in getattr(node, "parts", ()) or ():
        _walk(child, out, in_substitution=in_substitution)


def _handle_word_part(
    part: bashlex.ast.node,
    words: list[bashlex.ast.node],
    out: list[CanonicalLeaf],
) -> None:
    """Append word to accumulator and recurse into any command substitutions."""
    words.append(part)
    for sub in _substitutions_in(part):
        _walk(sub, out, in_substitution=True)


def _handle_redirect_part(
    part: bashlex.ast.node,
    redirects: list[bashlex.ast.node],
    out: list[CanonicalLeaf],
) -> None:
    """Append redirect to accumulator and recurse into substitutions in its target."""
    redirects.append(part)
    target = getattr(part, "output", None)
    if target is not None and getattr(target, "kind", None) == "word":
        for sub in _substitutions_in(target):
            # A command inside a redirect target (e.g. ``> >(tee log)``) is
            # still inside a substitution — mark it accordingly.
            _walk(sub, out, in_substitution=True)


def _partition_command_parts(
    parts: list[bashlex.ast.node],
    out: list[CanonicalLeaf],
) -> tuple[list[bashlex.ast.node], list[bashlex.ast.node]]:
    """Split command parts into (words, redirects), recursing into substitutions."""
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
            _handle_word_part(part, words, out)
        elif pkind == "redirect":
            _handle_redirect_part(part, redirects, out)
        # Anything else (operators etc.) ignored at this level.

    return words, redirects


def _process_command(
    node: bashlex.ast.node,
    out: list[CanonicalLeaf],
    *,
    in_substitution: bool = False,
) -> None:
    """Extract a CanonicalLeaf from a ``CommandNode`` and recurse into any
    command/process substitutions reachable from its words.

    ``in_substitution`` is forwarded from ``_walk`` and stored on the
    resulting :class:`CanonicalLeaf` as
    :attr:`~CanonicalLeaf.is_substitution_child`.
    """
    parts: list[bashlex.ast.node] = list(getattr(node, "parts", ()) or ())
    words, redirects = _partition_command_parts(parts, out)

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
            is_substitution_child=in_substitution,
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


def _is_positional_subcommand(token: str) -> bool:
    """Return True when *token* is a valid positional subcommand name (not a flag or substitution)."""
    return (
        bool(token)
        and not token.startswith("-")
        and not token.startswith(_SUBSTITUTION_PREFIXES)
    )


def _resolve_task_runner_subcommand(
    verb: str, words: list[bashlex.ast.node]
) -> tuple[str | None, int] | None:
    """Return (subcommand, positional_start) for task-runner verbs, or None if not a runner."""
    if len(words) >= 2:
        second = _word_literal(words[1])
        if (verb, second) in TASK_RUNNERS and len(words) >= 3:
            target = _word_literal(words[2])
            if _is_positional_subcommand(target):
                return target, 3
            return None, 2
    if (verb,) in TASK_RUNNERS and len(words) >= 2:
        target = _word_literal(words[1])
        if _is_positional_subcommand(target):
            return target, 2
        return None, 1
    return None  # not a task runner


def _resolve_two_word_subcommand(
    verb: str, words: list[bashlex.ast.node]
) -> tuple[str, int] | None:
    """Return ``("<group> <action>", 3)`` for two-word subcommand verbs.

    Returns ``None`` when the verb/group pair is not in
    :data:`TWO_WORD_SUBCOMMAND_VERBS` or when the third token is not a real
    positional (flag, substitution, or absent). The caller then falls through
    to single-word subcommand resolution.
    """
    if len(words) < 3:
        return None
    second = _word_literal(words[1])
    if not second or (verb, second) not in TWO_WORD_SUBCOMMAND_VERBS:
        return None
    third = _word_literal(words[2])
    if not _is_positional_subcommand(third):
        return None
    return f"{second} {third}", 3


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
    runner_result = _resolve_task_runner_subcommand(verb, words)
    if runner_result is not None:
        return runner_result

    # Content verbs: first positional is content (path/pattern/message/script),
    # not a subcommand. Collapse all invocations into one shape by discarding
    # the positional slot entirely.
    if verb in CONTENT_VERBS:
        return None, 1

    # Two-word subcommand verbs (vault, doppler) — group + action joined.
    # Checked before the default branch so the two-word form wins; falls
    # through to the default when the third token isn't a real positional.
    two_word_result = _resolve_two_word_subcommand(verb, words)
    if two_word_result is not None:
        return two_word_result

    # Default: first non-flag token after the verb.
    if len(words) >= 2:
        candidate = _word_literal(words[1])
        if candidate and not candidate.startswith("-"):
            # Process/command substitutions are not meaningful subcommand names
            # — drop the slot. Recursion into the inner command still happens
            # via _substitutions_in.
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
    stored on :attr:`CanonicalLeaf.positional_paths` and passed to
    :func:`to_pattern_form` to derive ``path_spec`` candidates.

    Conservative: any token that isn't clearly a flag or substitution ends
    up here, including URLs, hostnames, and non-filesystem args. That's OK
    — :func:`to_pattern_form` only substitutes tokens that start with ``/``
    and match a known context-variable prefix; unmatched tokens produce no
    ``path_spec`` variant and are otherwise ignored.
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
