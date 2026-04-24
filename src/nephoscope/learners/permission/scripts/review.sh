#!/bin/bash
# Per-axis permission review.
#
# For each eligible candidate from `learner propose`:
#
#   Axis 1 (verb)  — only when to_pattern_form finds a $VAR substitution in
#                    the verb (i.e. verb is an absolute path under HOME/CWD/
#                    PROJECT_ROOT). Prompts: literal / generalize / skip.
#
#   Axis 2 (paths) — path constraint for the rule_shape.path_spec.
#                    Prompts: a=any(NULL) / p=$PROJECT_ROOT/** / c=$CWD/** /
#                             h=$HOME/**  / numbered path_spec variants from
#                             to_pattern_form / s=skip.
#
#   Axis 3 (flags) — literal flags array vs wildcard "*".
#                    Prompts: l=literal / w=wildcard / s=skip.
#
#   Axis 4 (tier)  — permission tier.
#                    Prompts: g=global / p=project / s=session / skip / q=quit.
#
#   Post-promote   — if flags="*" was chosen and concrete sibling rules exist
#                    at the same tier: "Subsume N concrete sibling rule(s)? [Y/n]"
#                    Default yes; deletes the narrower rules (decision 8-15).
#
# Pipe-delimited proposals from `learner propose`:
#   verb|subcommand|flags_json|obs|sessions
# Empty subcommand field = NULL subcommand.

set -euo pipefail

PY=/tmp/claude/observability-phase8/observability/.venv/bin/python
LEARNER="$PY -m learners.permission.learner"
cd /tmp/claude/observability-phase8/observability

# ---------------------------------------------------------------------------
# Context: derive project_root for pattern substitution.
# Use OBSERVABILITY_DB env (or default) so the lookup targets the right DB.
# ---------------------------------------------------------------------------
CTX_HOME="${HOME:-}"
CTX_CWD="$(pwd)"
CTX_PROJECT_ROOT=""

# Look up project_root via scope module (best effort — empty on failure).
CTX_PROJECT_ROOT="$(
  $PY -c "
import sys
sys.path.insert(0, '/tmp/claude/observability-phase8/observability')
from lib.scope import resolve_project_root
r = resolve_project_root('$CTX_CWD')
print(r or '')
" 2>/dev/null || true)"

# Look up project_id and session_id for the current cwd.
_ctx_ids="$($LEARNER context-ids --cwd "$CTX_CWD" 2>/dev/null || echo 'project_id=
session_id=')"
CTX_PROJECT_ID="$(echo "$_ctx_ids" | grep '^project_id=' | cut -d= -f2)"
CTX_SESSION_ID="$(echo "$_ctx_ids"  | grep '^session_id='  | cut -d= -f2)"

# ---------------------------------------------------------------------------
# 1. Refresh candidates.
# ---------------------------------------------------------------------------
$LEARNER scan >/dev/null

# ---------------------------------------------------------------------------
# 2. Collect proposals.
# ---------------------------------------------------------------------------
proposals_file=$(mktemp)
trap 'rm -f "$proposals_file"' EXIT
$LEARNER propose > "$proposals_file"

if [ ! -s "$proposals_file" ]; then
  echo "no promotion candidates meet thresholds"
  exit 0
fi

total=$(wc -l < "$proposals_file")
echo "reviewing $total candidate(s) — (q)uit at any tier prompt to stop early"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_read_tty() {
  # Read one line from the TTY (interactive) or stdin (piped, for tests).
  if [ -t 0 ] && [ -r /dev/tty ]; then
    read -r _REPLY </dev/tty || _REPLY=""
  else
    read -r _REPLY || _REPLY=""
  fi
}

_json_field() {
  # Extract a single string field from a JSON object emitted to stdout.
  # Usage: _json_field <json> <field>
  local _json="$1" _field="$2"
  $PY -c "
import json, sys
d = json.loads(sys.argv[1])
v = d.get(sys.argv[2])
print(v if v is not None else '')
" "$_json" "$_field" 2>/dev/null || true
}

_json_list() {
  # Extract a JSON array field as newline-separated strings.
  local _json="$1" _field="$2"
  $PY -c "
import json, sys
d = json.loads(sys.argv[1])
for item in d.get(sys.argv[2], []):
    print(item)
" "$_json" "$_field" 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# Per-candidate loop
# ---------------------------------------------------------------------------
promoted=0
rejected=0
skipped=0

while IFS='|' read -r verb sub flags obs sessions <&3; do
  echo
  sub_disp="${sub:--}"
  echo "--- ${verb} ${sub_disp}  flags=${flags}  (obs=${obs}, sessions=${sessions}) ---"

  # ------------------------------------------------------------------
  # Compute pattern variants for this candidate.
  # ------------------------------------------------------------------
  _pv_args=(--verb "$verb" --flags "$flags")
  [ -n "$sub" ]             && _pv_args+=(--subcommand "$sub")
  [ -n "$CTX_HOME" ]        && _pv_args+=(--home "$CTX_HOME")
  [ -n "$CTX_CWD" ]         && _pv_args+=(--cwd "$CTX_CWD")
  [ -n "$CTX_PROJECT_ROOT" ] && _pv_args+=(--project-root "$CTX_PROJECT_ROOT")

  variants_json="$($LEARNER pattern-variants "${_pv_args[@]}" 2>/dev/null \
    || echo '{"verb_pattern":null,"path_specs":[],"flags_literal":"[]"}')"

  verb_pattern="$(_json_field "$variants_json" "verb_pattern")"

  # path_specs from to_pattern_form (only non-empty when verb is an abs path).
  mapfile -t _ps_from_variants < <(_json_list "$variants_json" "path_specs")

  # Build the full path-option menu, deduplicating across sources.
  _path_opts=()
  declare -A _ps_seen
  _add_ps_opt() {
    local _opt="$1"
    if [ -z "${_ps_seen[$_opt]:-}" ]; then
      _ps_seen[$_opt]=1
      _path_opts+=("$_opt")
    fi
  }

  for _ps in "${_ps_from_variants[@]}"; do
    [ -n "$_ps" ] && _add_ps_opt "$_ps"
  done
  [ -n "$CTX_PROJECT_ROOT" ] && _add_ps_opt "\$PROJECT_ROOT/**"
  [ -n "$CTX_CWD" ]          && _add_ps_opt "\$CWD/**"
  [ -n "$CTX_HOME" ]         && _add_ps_opt "\$HOME/**"
  unset _ps_seen

  # ------------------------------------------------------------------
  # Axis 1: Verb (only when a $VAR pattern exists).
  # ------------------------------------------------------------------
  chosen_verb="$verb"
  if [ -n "$verb_pattern" ] && [ "$verb_pattern" != "$verb" ]; then
    printf "  Verb:  literal=%-40s  pattern=%s\n" "$verb" "$verb_pattern"
    printf "         [l=literal / g=generalize / s=skip]: "
    _read_tty
    case "$_REPLY" in
      g|G) chosen_verb="$verb_pattern" ;;
      s|S) skipped=$((skipped + 1)); continue ;;
      *)   chosen_verb="$verb" ;;   # default: literal
    esac
  fi

  # ------------------------------------------------------------------
  # Axis 2: Paths.
  # ------------------------------------------------------------------
  chosen_path_spec_arg=()   # empty = omit --path-spec (NULL = any)
  printf "  Paths: [a=any"
  _idx=1
  for _opt in "${_path_opts[@]}"; do
    printf " / %d=%s" "$_idx" "$_opt"
    _idx=$((_idx + 1))
  done
  printf " / s=skip]: "
  _read_tty

  case "$_REPLY" in
    a|A|"")
      # any — omit --path-spec (NULL)
      chosen_path_spec_arg=()
      ;;
    s|S)
      skipped=$((skipped + 1))
      continue
      ;;
    [1-9])
      _chosen_idx=$((_REPLY - 1))
      if [ "$_chosen_idx" -lt "${#_path_opts[@]}" ]; then
        chosen_path_spec_arg=(--path-spec "${_path_opts[$_chosen_idx]}")
      fi
      ;;
    *)
      # Unrecognised — treat as any.
      chosen_path_spec_arg=()
      ;;
  esac

  # ------------------------------------------------------------------
  # Axis 3: Flags.
  # ------------------------------------------------------------------
  chosen_flags="$flags"
  printf "  Flags: %s  [l=literal / w=wildcard(*) / s=skip]: " "$flags"
  _read_tty
  case "$_REPLY" in
    w|W) chosen_flags="*" ;;
    s|S) skipped=$((skipped + 1)); continue ;;
    *)   chosen_flags="$flags" ;;   # default: literal
  esac

  # ------------------------------------------------------------------
  # Axis 4: Tier.
  # ------------------------------------------------------------------
  chosen_tier=""
  chosen_tier_args=()
  printf "  Tier:  [g=global / p=project / s=session / skip / q=quit]: "
  _read_tty
  case "$_REPLY" in
    g|G|"")
      chosen_tier="global"
      chosen_tier_args=(--tier global)
      ;;
    p|P)
      if [ -z "$CTX_PROJECT_ID" ]; then
        echo "  (no project record for cwd=$CTX_CWD — skipping)"
        skipped=$((skipped + 1))
        continue
      fi
      chosen_tier="project"
      chosen_tier_args=(--tier project --project-id "$CTX_PROJECT_ID")
      ;;
    s|S)
      if [ -z "$CTX_SESSION_ID" ]; then
        echo "  (no session record for cwd=$CTX_CWD — skipping)"
        skipped=$((skipped + 1))
        continue
      fi
      chosen_tier="session"
      chosen_tier_args=(--tier session --session-id "$CTX_SESSION_ID")
      ;;
    skip)
      skipped=$((skipped + 1))
      continue
      ;;
    q|Q)
      echo "quitting"
      break
      ;;
    *)
      # Default: global.
      chosen_tier="global"
      chosen_tier_args=(--tier global)
      ;;
  esac

  # ------------------------------------------------------------------
  # Promote.
  # ------------------------------------------------------------------
  _promote_args=(--verb "$chosen_verb" --flags "$chosen_flags")
  [ -n "$sub" ] && _promote_args+=(--subcommand "$sub")
  _promote_args+=("${chosen_tier_args[@]}")
  _promote_args+=("${chosen_path_spec_arg[@]}")

  _promote_stderr_file=$(mktemp)
  if $LEARNER promote --sync "${_promote_args[@]}" 2>"$_promote_stderr_file"; then
    rm -f "$_promote_stderr_file"
    promoted=$((promoted + 1))

    # ------------------------------------------------------------------
    # Subsume sibling concrete rules when flags=* was chosen.
    # ------------------------------------------------------------------
    if [ "$chosen_flags" = "*" ]; then
      _sibling_args=(--verb "$verb")
      [ -n "$sub" ] && _sibling_args+=(--subcommand "$sub")
      _sibling_args+=("${chosen_tier_args[@]}")

      _sibling_count="$($LEARNER count-concrete-siblings "${_sibling_args[@]}" 2>/dev/null || echo 0)"

      if [ "${_sibling_count:-0}" -gt 0 ]; then
        printf "  Subsume %s concrete sibling rule(s)? [Y/n]: " "$_sibling_count"
        _read_tty
        case "$_REPLY" in
          n|N)
            echo "  (kept sibling rules)"
            ;;
          *)
            # Default yes.
            $LEARNER subsume-siblings "${_sibling_args[@]}"
            ;;
        esac
      fi
    fi
  else
    # Check if failure was a MirrorHashMismatch (external edit detected).
    if grep -qi "edited externally\|hash mismatch" "$_promote_stderr_file" 2>/dev/null; then
      rm -f "$_promote_stderr_file"
      echo "Settings file modified externally. Run '/nephoscope:permissions reconcile' and retry."
      exit 1
    fi
    cat "$_promote_stderr_file" >&2
    rm -f "$_promote_stderr_file"
    echo "  (promotion failed — skipping)"
    skipped=$((skipped + 1))
  fi

done 3< "$proposals_file"

echo
echo "summary: promoted ${promoted}, rejected ${rejected}, skipped ${skipped}"
