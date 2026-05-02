from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

_GUIDE_BASE = "docs/auto-approve-evaluation-guide.md"

WRAPPERS: frozenset[str] = frozenset(
    {
        "env",
        "xargs",
        "sudo",
        "su",
        "time",
        "timeout",
        "ionice",
        "nice",
        "nohup",
        "setsid",
        "strace",
        "ltrace",
        "nsenter",
        "unshare",
        "firejail",
        "doas",
    }
)

MUTATING_VERBS: frozenset[str] = frozenset(
    {
        "rm",
        "chmod",
        "cp",
        "mv",
        "tee",
        "dd",
        "install",
        "ln",
        "truncate",
        "shred",
        "rsync",
        "chown",
        "chgrp",
        "mkfs",
    }
)

DANGEROUS_FLAGS: dict[str, frozenset[str]] = {
    "curl": frozenset({"-o", "--output", "-O", "--remote-name", "-J"}),
    "find": frozenset({"-delete", "--delete", "-exec", "-execdir"}),
    "chmod": frozenset({"-R", "--recursive"}),
    "wget": frozenset({"-O", "--output-document", "-P", "--directory-prefix"}),
    "rm": frozenset({"-r", "-rf", "-f", "--recursive", "--force"}),
}


@dataclass(frozen=True)
class Finding:
    severity: Literal["WARN", "DANGER"]
    code: str
    message: str
    guide_anchor: str


def evaluate(
    verb: str,
    flags_json: str,
    subcommand: str | None,
    path_spec: str | None,
) -> list[Finding]:
    """Evaluate a candidate permission rule for safety issues.

    Args:
        verb: The command verb (e.g. 'env', 'rm', 'curl').
        flags_json: JSON-encoded flags string: '*' for wildcard, '["--foo"]'
            for a list, or '[]' for no flags.
        subcommand: The subcommand constraint, or None if unconstrained.
        path_spec: The path constraint, or None if unconstrained.

    Returns:
        A list of Finding objects. Empty list means no issues found.
    """
    findings: list[Finding] = []

    # Check 1 — transparent wrapper wildcard
    if verb in WRAPPERS and flags_json == "*" and subcommand is None:
        findings.append(
            Finding(
                severity="DANGER",
                code="transparent_wrapper_wildcard",
                message=(
                    f'flags: "*" on the transparent wrapper {verb!r} with no subcommand '
                    f"constraint approves everything that wrapper can run — including "
                    f'destructive commands like "{verb} rm -rf /". '
                    f"Drop this rule and approve the wrapped verb directly instead."
                ),
                guide_anchor=f"{_GUIDE_BASE}#transparent-wrappers",
            )
        )

    # Check 2 — wildcard hides dangerous flag
    if flags_json == "*" and path_spec is None and verb in DANGEROUS_FLAGS:
        dangerous = sorted(DANGEROUS_FLAGS[verb])
        findings.append(
            Finding(
                severity="DANGER",
                code="wildcard_hides_dangerous_flag",
                message=(
                    f'flags: "*" on {verb!r} covers dangerous flags '
                    f"{dangerous}. Add path_spec to limit scope or enumerate "
                    f"only the safe flags you intend to allow."
                ),
                guide_anchor=f"{_GUIDE_BASE}#wildcard-dangerous-flags",
            )
        )

    # Check 3 — mutating verb, no path_spec
    if verb in MUTATING_VERBS and path_spec is None:
        findings.append(
            Finding(
                severity="WARN",
                code="mutating_verb_no_path_spec",
                message=(
                    f"{verb!r} can permanently change or delete files; without "
                    f"path_spec the rule is global. Consider adding a path_spec "
                    f"to limit the blast radius."
                ),
                guide_anchor=f"{_GUIDE_BASE}#blast-radius",
            )
        )

    return findings
