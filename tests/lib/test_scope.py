"""Tests for lib.scope: project root resolution + path extraction."""

from __future__ import annotations

import subprocess
from pathlib import Path

from nephoscope.lib import scope


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
