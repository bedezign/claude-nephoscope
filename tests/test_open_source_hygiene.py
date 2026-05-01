"""Open-source hygiene test: audit the repo for internal references.

Walks the nephoscope source tree and grep-checks for substrings that would
expose the plugin as coupled to private infrastructure. Static-only: does
not catch dynamic imports, f-string-constructed paths, or intentional
fixture data under ``tests/``.

Forbidden substrings (in priority order):

1. ``BookStack`` / ``bookstack`` / ``wiki.bedezign`` — wiki infrastructure.
2. ``/home/steve/`` — absolute paths specific to the original author.
3. ``dot_claude`` — sibling private repo name.
4. ``rules/<kebab>.md`` — references to private rule pages.
5. ``claude-peers`` — peer MCP server (not a functional dependency).
6. ``continuous-learning-v2`` — predecessor skill.
7. ``Phase [0-9]`` / ``WP[0-9]+`` / ``Wave [0-9]+`` / ``W[0-9]+[A-Z]`` —
   internal phase, work-package, and wave nomenclature.

Expected state: zero hits across the source tree.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


FORBIDDEN_RE = (
    r"BookStack"
    r"|bookstack"
    r"|wiki\.bedezign"
    r"|/home/steve"
    r"|dot_claude"
    r"|rules/[a-z-]*\.md"
    r"|claude-peers"
    r"|continuous-learning-v2"
    r"|wiki\.bedezign\.casa"
    r"|Phase [0-9]"
    r"|WP[0-9]+"
    r"|Wave [0-9]+"
    r"|\bW[0-9]+[A-Z]\b"
)


def test_no_forbidden_substrings():
    """Grep walk: all tracked source files except ``tests/`` and caches.

    Assertions:
    1. At least 15 files walked (discoverability guard; ``tests/`` excluded).
    2. Zero hits on any forbidden substring.

    ``tests/`` is excluded because test fixtures intentionally contain
    literal absolute paths for path-parsing coverage.
    """
    repo_root = Path(__file__).resolve().parent.parent

    cmd = [
        "grep",
        "-r",
        "--include=*.py",
        "--include=*.sh",
        "--include=*.md",
        "--include=*.sql",
        "--include=*.yaml",
        "--include=*.toml",
        "--include=*.json",
        "-E",
        FORBIDDEN_RE,
        "--exclude-dir=tests",
        "--exclude-dir=.venv",
        "--exclude-dir=.venv-step4",
        "--exclude-dir=__pycache__",
        "--exclude-dir=.git",
        "--exclude-dir=.codeatlas",
        str(repo_root),
    ]

    result = subprocess.run(
        cmd,
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )

    find_cmd = [
        "find",
        str(repo_root),
        "-type",
        "f",
        "(",
        "-name",
        "*.py",
        "-o",
        "-name",
        "*.sh",
        "-o",
        "-name",
        "*.md",
        "-o",
        "-name",
        "*.sql",
        "-o",
        "-name",
        "*.yaml",
        "-o",
        "-name",
        "*.toml",
        "-o",
        "-name",
        "*.json",
        ")",
        "-not",
        "-path",
        "*/__pycache__/*",
        "-not",
        "-path",
        "*/.venv/*",
        "-not",
        "-path",
        "*/.venv-*/*",
        "-not",
        "-path",
        "*/tests/*",
        "-not",
        "-path",
        "*/.git/*",
        "-not",
        "-path",
        "*/.codeatlas/*",
    ]
    find_result = subprocess.run(find_cmd, capture_output=True, text=True)
    files_walked = len([f for f in find_result.stdout.strip().split("\n") if f])

    assert files_walked >= 15, (
        f"Only {files_walked} files walked. Refactor may have hidden the module tree."
    )

    hits = result.stdout.strip()
    assert not hits, f"Found {len(hits.splitlines())} forbidden substring hits:\n{hits}"


def test_hygiene_walker_detects_fixtures(tmp_path):
    """Self-test: walker correctly detects forbidden substrings when present.

    If this test ever silently passes without matching, the live-source pass
    is hollow — the regex has drifted out of sync with itself.
    """
    test_strings = [
        "# BookStack reference here",
        "# bookstack reference here",
        "# wiki.bedezign reference here",
        "# path /home/steve/project",
        "# dot_claude module reference",
        "# see rules/testing.md",
        "# claude-peers integration needed",
        "# continuous-learning-v2 predecessor",
        "# wiki.bedezign.casa link",
        "# Phase 8.5 rewrite",
        "# WP7 wrap-up",
        "# Wave 4 follow-up",
        "# W1A deliverable",
    ]

    for forbidden_str in test_strings:
        fixture = tmp_path / "fixture.txt"
        fixture.write_text(forbidden_str)

        result = subprocess.run(
            ["grep", "-E", FORBIDDEN_RE, str(fixture)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"Walker failed to detect forbidden substring: {forbidden_str!r}"
        )
