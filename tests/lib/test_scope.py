"""Tests for lib.scope: project root resolution + path classification."""

from __future__ import annotations

import subprocess
from pathlib import Path

from lib import scope


# ---------------------------------------------------------------------------
# resolve_project_root
# ---------------------------------------------------------------------------


def test_empty_cwd_returns_none():
    assert scope.resolve_project_root("") is None


def test_repository_suffix_strips_to_parent(tmp_path):
    workspace = tmp_path / "myproj"
    (workspace / "repository").mkdir(parents=True)
    assert scope.resolve_project_root(str(workspace / "repository")) == str(workspace)


def test_non_repo_non_repository_suffix_returns_cwd_itself(tmp_path):
    # Not a git repo, not named "repository" — rule 3 falls back to cwd.
    # Use a dir outside any existing git repo to avoid rule 2 triggering.
    cwd = tmp_path / "standalone"
    cwd.mkdir()
    result = scope.resolve_project_root(str(cwd))
    # On tmp_path there's no git repo — should return cwd.
    assert result == str(cwd)


def test_git_toplevel_rule(tmp_path):
    # Create a minimal git repo and assert rule 2 picks up the toplevel.
    repo = tmp_path / "somerepo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    sub = repo / "src" / "module"
    sub.mkdir(parents=True)
    resolved = scope.resolve_project_root(str(sub))
    assert resolved is not None
    # git toplevel returns the repo root (resolve symlinks to match realpath)
    assert Path(resolved).resolve() == repo.resolve()


def test_repository_suffix_wins_over_git_toplevel(tmp_path):
    # If cwd ends in /repository, rule 1 fires even when rule 2 could.
    workspace = tmp_path / "proj"
    repo_dir = workspace / "repository"
    repo_dir.mkdir(parents=True)
    subprocess.run(["git", "-C", str(repo_dir), "init", "-q"], check=True)
    # Rule 1 returns workspace, not the git toplevel (repo_dir itself).
    assert scope.resolve_project_root(str(repo_dir)) == str(workspace)


# ---------------------------------------------------------------------------
# classify_paths
# ---------------------------------------------------------------------------


def test_no_paths_returns_no_path():
    assert scope.classify_paths([], "/project") == scope.NO_PATH


def test_no_root_returns_no_path():
    assert scope.classify_paths(["/some/path"], None) == scope.NO_PATH
    assert scope.classify_paths(["/some/path"], "") == scope.NO_PATH


def test_single_path_inside_root(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "file.py").touch()
    assert (
        scope.classify_paths([str(root / "file.py")], str(root)) == scope.WITHIN_PROJECT
    )


def test_single_path_outside_root(tmp_path):
    root = tmp_path / "proj"
    other = tmp_path / "other"
    root.mkdir()
    other.mkdir()
    (other / "file.py").touch()
    assert (
        scope.classify_paths([str(other / "file.py")], str(root))
        == scope.OUTSIDE_PROJECT
    )


def test_mixed_paths(tmp_path):
    root = tmp_path / "proj"
    other = tmp_path / "other"
    root.mkdir()
    other.mkdir()
    inside = root / "a.py"
    outside = other / "b.py"
    inside.touch()
    outside.touch()
    assert scope.classify_paths([str(inside), str(outside)], str(root)) == scope.MIXED


def test_path_equal_to_root_is_within(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    assert scope.classify_paths([str(root)], str(root)) == scope.WITHIN_PROJECT


def test_sibling_prefix_is_not_within(tmp_path):
    # /foo/bar vs /foo/bar-extra — the prefix check must use trailing slash
    # so "bar-extra" isn't mistaken for a child of "bar".
    root = tmp_path / "bar"
    sibling = tmp_path / "bar-extra"
    root.mkdir()
    sibling.mkdir()
    (sibling / "file.py").touch()
    assert (
        scope.classify_paths([str(sibling / "file.py")], str(root))
        == scope.OUTSIDE_PROJECT
    )


def test_relative_path_resolved_against_cwd(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "file.py").touch()
    monkeypatch.chdir(root)
    # "./file.py" resolves to root/file.py which IS inside root.
    assert scope.classify_paths(["./file.py"], str(root)) == scope.WITHIN_PROJECT


# ---------------------------------------------------------------------------
# paths_for_tool_call
# ---------------------------------------------------------------------------


def test_read_extracts_file_path():
    assert scope.paths_for_tool_call("Read", {"file_path": "/a/b.py"}) == ["/a/b.py"]


def test_edit_extracts_file_path():
    assert scope.paths_for_tool_call("Edit", {"file_path": "/a/b.py"}) == ["/a/b.py"]


def test_grep_extracts_path():
    assert scope.paths_for_tool_call("Grep", {"path": "/x", "pattern": "foo"}) == ["/x"]


def test_glob_with_no_path_returns_empty():
    # Glob without an explicit path prop has no paths to classify.
    assert scope.paths_for_tool_call("Glob", {"pattern": "**/*.py"}) == []


def test_bash_returns_empty_list():
    # Bash is handled specially by the recorder (via canonicalize), not here.
    assert scope.paths_for_tool_call("Bash", {"command": "rm foo"}) == []


def test_unknown_tool_returns_empty():
    assert scope.paths_for_tool_call("WebFetch", {"url": "https://x"}) == []


def test_non_dict_input_returns_empty():
    assert scope.paths_for_tool_call("Read", "not-a-dict") == []  # type: ignore[arg-type]


def test_notebook_edit_prefers_notebook_path():
    # NotebookEdit has both notebook_path and file_path possible; both captured
    # in declaration order.
    got = scope.paths_for_tool_call(
        "NotebookEdit",
        {"notebook_path": "/a.ipynb", "file_path": "/b.ipynb"},
    )
    assert got == ["/a.ipynb", "/b.ipynb"]
