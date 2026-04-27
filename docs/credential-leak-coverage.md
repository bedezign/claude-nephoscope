# Credential-leak coverage

Nephoscope ships with a default set of deny rules covering the most common ways credentials leak into Claude Code's conversation transcript. These rules are loaded automatically on first install and applied globally. You can disable any rule if you have a legitimate use case.

## What's covered

### Credential file reads

Any command reading well-known credential files is blocked — `cat`, `grep`, `head`, `less`, `tail`, `view`, and everything else. A single rule covers all verbs, matched at the command's AST level.

Files blocked:
- `.env`, `.env.local`, `.env.development`, `.env.production`, `.env.staging`, `.env.test` — application secrets at any depth (`apps/web/.env` is caught too). `.env.example` and `.env.template` are deliberately exempt.
- `~/.aws/credentials` — AWS access keys
- `~/.kube/config` — Kubernetes bearer tokens and client certificates
- `~/.docker/config.json` — Docker registry authentication (base64-encoded)
- `~/.npmrc` — npm publish tokens
- `~/.netrc` — FTP and curl credentials (plaintext)
- `~/.bash_history` and `~/.zsh_history` — Shell history (reveals previously-typed credentials and sensitive commands)

**Example:** `cat ~/.aws/credentials` is blocked. So is `grep AKIAIOSFODNN7EXAMPLE ~/.aws/credentials`. The rule does not distinguish between them — it matches based on the file path, not the command verb.

### Secret-manager standalone reads

Secret-manager CLIs like `1Password`, `Vault`, `Bitwarden`, `Doppler`, and `pass` are blocked when run standalone, because they print the secret value to stdout — which becomes part of the conversation transcript.

The safe form — passing the output directly to another command via command substitution — is **unaffected**:

```bash
# BLOCKED — secret is printed:
op read 'op://Vault/Item/field'

# ALLOWED — secret flows directly to curl without appearing in the transcript:
curl -H "Authorization: Bearer $(op read 'op://Vault/Item/token')"
```

Commands blocked in standalone form (allowed when used inline with `$(...)`):
- `op read` — 1Password CLI
- `vault kv get` — HashiCorp Vault
- `bw get` — Bitwarden CLI
- `doppler secrets get` — Doppler
- `pass show` — passwordstore
- `gopass show` — gopass

## What's not covered yet

**Hardcoded credential literals in command lines.** If you accidentally paste `curl -H "Authorization: Bearer sk_live_abcd1234..."` into a chat, nephoscope's current rules won't catch it. Detecting this requires entropy scanning and known-prefix regex matching rather than command-shape matching. This is a known gap and may land in a future release as a separate secret-scanner module.

**Environment dumps.** Commands like `env`, `printenv`, and `ps auxe` print every environment variable, which on most developer machines includes API keys and database URLs. We don't ship default rules for these because the shape is genuinely hard to express without over-blocking legitimate use — `env FOO=bar cmd args` is a perfectly normal launch wrapper, not a leak. A future release may add narrower rules (e.g. `env` standalone with no following command) once the canonicalisation can distinguish the shapes cleanly.

## Customising or removing the defaults

The defaults live as global-tier `rejected` permissions in nephoscope's database, just like any other rule. You can list them, disable specific ones, or clear them all.

**List the defaults:**

```
/nephoscope:permissions list
```

You'll see each rule with its reason and tier. All credential-leak rules show as `source: seed`.

**Remove a specific rule** — for example, if you have a legitimate need to read your shell history:

```
/nephoscope:permissions unpermit --verb '*' --path-spec '$HOME/.bash_history' --tier global
```

After that command, `cat ~/.bash_history` will no longer be blocked at the global level (though you may have project- or session-level rules that still block it).

Once removed, rules stay removed for that tier. The next nephoscope session will not re-add them. If you change your mind, you can re-create a rule by hand using the same `promote` command pattern, or reinstall the plugin to re-seed the defaults.
