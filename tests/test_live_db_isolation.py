"""Sentinel test: the test suite must not write to the live observations DB.

This guards against the Phase 8.5 regression where a pre-refactor test fixture
(`lib.db.DB_PATH` captured at module import, `monkeypatch.setenv` therefore a
no-op) caused test runs to hit ``~/.cache/claude/observability/observations.db``
and wipe 3,200+ rows of real history.

The test is a cheap canary — it checks that the live DB file's sha256 is
byte-identical before and after the test-collection phase, which is the only
window during which a rogue fixture could silently connect to live and mutate
it. A fail here means something in the suite is still import-time-reading
``OBSERVABILITY_DB`` or otherwise bypassing the ``tmp_db`` fixture.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest


LIVE_DB_PATH = Path.home() / ".cache" / "claude" / "observability" / "observations.db"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


@pytest.mark.skipif(
    not LIVE_DB_PATH.exists(),
    reason="no live DB on this host — canary is not applicable",
)
def test_live_db_unchanged_during_collection():
    """Live DB must be byte-identical between pytest collection and this test.

    Runs once per session via a conftest-level helper that records the pre-sha
    at collection time. If any test between collection and this runs wrote to
    live, the sha will differ.
    """
    pre_sha = os.environ.get("_LIVE_DB_SHA_AT_COLLECT")
    if pre_sha is None:
        pytest.skip("conftest did not record pre-collection sha; canary skipped")
    post_sha = _sha256(LIVE_DB_PATH)
    assert pre_sha == post_sha, (
        f"live DB was modified during the test run — a fixture leaked to "
        f"{LIVE_DB_PATH}. pre={pre_sha[:16]} post={post_sha[:16]}"
    )
