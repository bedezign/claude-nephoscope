"""Tests for generalize_path_spec() cross-project path suggestion.

Covers:
  - Empty / below-minimum paths → None
  - All under project root (≥90%) → '$PROJECT_ROOT/**'
  - Below threshold under project root → None
  - All under home, no project root → '$HOME/**'
  - Project root nested under home: project root wins
  - Split across multiple project roots (neither ≥90%) → None
  - Empty known_roots with home coverage → '$HOME/**'
  - Empty known_roots + no home coverage → None
  - No known_roots + no home → None
  - Exact boundary: 9/10 under root (90%) → '$PROJECT_ROOT/**'
  - Just below boundary: 8/10 under root (80%) → None
"""

from __future__ import annotations


from nephoscope.learners.permission.learner import generalize_path_spec


# ---------------------------------------------------------------------------
# Below minimum path count
# ---------------------------------------------------------------------------


def test_empty_paths_returns_none():
    assert generalize_path_spec((), frozenset(), "") is None


def test_one_path_returns_none():
    assert (
        generalize_path_spec(("/work/proj/file.py",), frozenset({"/work/proj"}), "")
        is None
    )


def test_two_paths_returns_none():
    paths = ("/work/proj/a.py", "/work/proj/b.py")
    assert generalize_path_spec(paths, frozenset({"/work/proj"}), "") is None


# ---------------------------------------------------------------------------
# Project root coverage
# ---------------------------------------------------------------------------


def test_three_paths_all_under_root_returns_project_root():
    paths = ("/work/proj/a.py", "/work/proj/b.py", "/work/proj/c.py")
    roots = frozenset({"/work/proj"})
    assert generalize_path_spec(paths, roots, "/home/user") == "$PROJECT_ROOT/**"


def test_two_of_three_paths_under_root_returns_none():
    """2/3 = 66% < 90% → None."""
    paths = ("/work/proj/a.py", "/work/proj/b.py", "/tmp/c.py")
    roots = frozenset({"/work/proj"})
    assert generalize_path_spec(paths, roots, "/home/user") is None


def test_nine_of_ten_paths_under_root_returns_project_root():
    """9/10 = 90% → '$PROJECT_ROOT/**'."""
    root = "/work/proj"
    paths = tuple(f"{root}/f{i}.py" for i in range(9)) + ("/tmp/outside.py",)
    assert (
        generalize_path_spec(paths, frozenset({root}), "/home/user")
        == "$PROJECT_ROOT/**"
    )


def test_eight_of_ten_paths_under_root_returns_none():
    """8/10 = 80% < 90% → None."""
    root = "/work/proj"
    paths = tuple(f"{root}/f{i}.py" for i in range(8)) + ("/tmp/x.py", "/tmp/y.py")
    assert generalize_path_spec(paths, frozenset({root}), "/home/user") is None


# ---------------------------------------------------------------------------
# Home coverage (fallback when no project root covers ≥90%)
# ---------------------------------------------------------------------------


def test_three_paths_all_under_home_no_roots_returns_home():
    paths = ("/home/user/a.py", "/home/user/b.py", "/home/user/c.py")
    assert generalize_path_spec(paths, frozenset(), "/home/user") == "$HOME/**"


def test_two_of_three_paths_under_home_returns_none():
    """2/3 = 66% < 90% → None even for home."""
    paths = ("/home/user/a.py", "/home/user/b.py", "/tmp/c.py")
    assert generalize_path_spec(paths, frozenset(), "/home/user") is None


# ---------------------------------------------------------------------------
# Project root nested under home: project root is preferred
# ---------------------------------------------------------------------------


def test_project_root_nested_under_home_returns_project_root():
    """Paths under /home/user/work/proj qualify for both home and project root.

    Project root is checked first and should win.
    """
    home = "/home/user"
    root = "/home/user/work/proj"
    paths = (
        f"{root}/a.py",
        f"{root}/b.py",
        f"{root}/src/c.py",
    )
    result = generalize_path_spec(paths, frozenset({root}), home)
    assert result == "$PROJECT_ROOT/**"


# ---------------------------------------------------------------------------
# Paths split across multiple project roots (no single root covers ≥90%)
# ---------------------------------------------------------------------------


def test_paths_split_across_two_roots_returns_none():
    """5 paths under root A, 5 under root B: 50% each → None."""
    root_a = "/work/proj-a"
    root_b = "/work/proj-b"
    paths = tuple(f"{root_a}/f{i}.py" for i in range(5)) + tuple(
        f"{root_b}/f{i}.py" for i in range(5)
    )
    roots = frozenset({root_a, root_b})
    assert generalize_path_spec(paths, roots, "/home/user") is None


# ---------------------------------------------------------------------------
# No home string provided
# ---------------------------------------------------------------------------


def test_empty_home_does_not_suggest_home():
    """Home suggestion is skipped when home is an empty string."""
    paths = ("/home/user/a.py", "/home/user/b.py", "/home/user/c.py")
    assert generalize_path_spec(paths, frozenset(), "") is None


# ---------------------------------------------------------------------------
# No known roots, coverage under home
# ---------------------------------------------------------------------------


def test_no_known_roots_falls_back_to_home():
    paths = ("/home/user/scripts/a.sh", "/home/user/scripts/b.sh", "/home/user/c.sh")
    assert generalize_path_spec(paths, frozenset(), "/home/user") == "$HOME/**"


# ---------------------------------------------------------------------------
# Mixed: some paths outside both root and home → root still wins if ≥90%
# ---------------------------------------------------------------------------


def test_mostly_under_root_with_some_outside_returns_project_root():
    root = "/work/proj"
    paths = (
        f"{root}/a.py",
        f"{root}/b.py",
        f"{root}/c.py",
        f"{root}/d.py",
        f"{root}/e.py",
        f"{root}/f.py",
        f"{root}/g.py",
        f"{root}/h.py",
        f"{root}/i.py",
        "/external/other.py",  # 9/10 = 90%
    )
    result = generalize_path_spec(paths, frozenset({root}), "/home/user")
    assert result == "$PROJECT_ROOT/**"
