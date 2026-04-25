"""Canonical permissions-slice SHA-256 for settings.json blobs.

Hashes only the sorted {allow, deny, ask} arrays under the top-level
`permissions` key.  Everything else in the file (hooks, env, model, …)
is the user's domain and must not cause spurious hash mismatches.
"""

from __future__ import annotations

import hashlib
import json


def settings_permissions_hash(content: bytes) -> str:
    """sha256 of the canonical permissions slice of a settings.json blob.

    Parse errors (json.JSONDecodeError) propagate to the caller unchanged.
    Missing or null permissions keys / array values are treated as empty lists.
    Duplicate entries are preserved (sorted only, not deduplicated).
    """
    data = json.loads(content)  # parse errors propagate to the caller
    perms = data.get("permissions") or {}
    canonical = {
        "allow": sorted(perms.get("allow") or []),
        "deny": sorted(perms.get("deny") or []),
        "ask": sorted(perms.get("ask") or []),
    }
    rendered = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()
