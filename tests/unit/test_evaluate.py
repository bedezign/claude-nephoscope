from __future__ import annotations


from nephoscope.learners.permission.evaluate import evaluate


# ---------------------------------------------------------------------------
# Check 1: transparent wrapper wildcard
# ---------------------------------------------------------------------------


def test_wrapper_wildcard_no_subcommand_is_danger() -> None:
    findings = evaluate("env", "*", subcommand=None, path_spec=None)
    codes = [f.code for f in findings]
    assert "transparent_wrapper_wildcard" in codes
    danger = [f for f in findings if f.code == "transparent_wrapper_wildcard"]
    assert danger[0].severity == "DANGER"


def test_wrapper_wildcard_with_subcommand_no_danger() -> None:
    # subcommand constrains the wrapper — no transparent-wrapper DANGER
    findings = evaluate("env", "*", subcommand="ls", path_spec=None)
    codes = [f.code for f in findings]
    assert "transparent_wrapper_wildcard" not in codes


def test_wrapper_specific_flags_no_danger() -> None:
    # specific flags, not wildcard — Check 1 does not fire
    findings = evaluate("env", '["-u"]', subcommand=None, path_spec=None)
    codes = [f.code for f in findings]
    assert "transparent_wrapper_wildcard" not in codes


# ---------------------------------------------------------------------------
# Check 2: wildcard hides dangerous flag
# ---------------------------------------------------------------------------


def test_curl_wildcard_no_path_spec_is_danger() -> None:
    findings = evaluate("curl", "*", subcommand=None, path_spec=None)
    codes = [f.code for f in findings]
    assert "wildcard_hides_dangerous_flag" in codes
    danger = [f for f in findings if f.code == "wildcard_hides_dangerous_flag"]
    assert danger[0].severity == "DANGER"


def test_curl_wildcard_with_path_spec_no_danger() -> None:
    # path_spec constrains curl — Check 2 does not fire
    findings = evaluate("curl", "*", subcommand=None, path_spec="$PROJECT_ROOT/**")
    codes = [f.code for f in findings]
    assert "wildcard_hides_dangerous_flag" not in codes


# ---------------------------------------------------------------------------
# Check 3: mutating verb, no path_spec
# ---------------------------------------------------------------------------


def test_rm_no_path_spec_is_warn() -> None:
    # Use non-wildcard flags so Check 2 does not fire; only WARN expected
    findings = evaluate("rm", '["-i"]', subcommand=None, path_spec=None)
    codes = [f.code for f in findings]
    assert "mutating_verb_no_path_spec" in codes
    warn = [f for f in findings if f.code == "mutating_verb_no_path_spec"]
    assert warn[0].severity == "WARN"
    # No DANGER for non-wildcard flags on rm
    assert not any(f.severity == "DANGER" for f in findings)


def test_rm_with_path_spec_no_warn() -> None:
    findings = evaluate("rm", '["-i"]', subcommand=None, path_spec="$PROJECT_ROOT/**")
    codes = [f.code for f in findings]
    assert "mutating_verb_no_path_spec" not in codes


# ---------------------------------------------------------------------------
# Clean verbs — no findings expected
# ---------------------------------------------------------------------------


def test_stat_wildcard_no_findings() -> None:
    # stat is read-only, not a wrapper, not mutating — all checks clean
    findings = evaluate("stat", "*", subcommand=None, path_spec=None)
    assert findings == []


def test_env_specific_flag_no_danger() -> None:
    # env with specific flag (not wildcard) — transparent-wrapper check does
    # not fire because flags_json != "*"
    findings = evaluate("env", '["-u"]', subcommand=None, path_spec=None)
    codes = [f.code for f in findings]
    assert "transparent_wrapper_wildcard" not in codes
