"""The operator's boards-seen cursor.

Unread on a board is "newer than the highest post the operator marked seen",
not unread-by-agents -- agents each have their own `since` cursor on the board
object. These tests pin the cursor's behaviour daemon-side: one-way advance,
count over all retained posts, and a clean miss for unknown oids.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from capwrap.daemon import Daemon


@pytest.fixture
async def daemon(state_dir):
    d = Daemon(audit_path=Path(state_dir) / "audit.db")
    yield d
    await d.shutdown()


def board(daemon, topic: str = "ops"):
    """The single board the test made, looked up by topic (ids are dynamic)."""
    matches = [b for b in daemon.kernel.boards() if b.topic == topic]
    assert len(matches) == 1
    return matches[0]


async def test_unread_starts_at_the_full_count(daemon):
    """A fresh operator has seen nothing, so every retained post is unread."""
    daemon.kernel.create_board("ops", created_by="operator")
    ops = board(daemon)
    ops.post("alpha", "one")
    ops.post("alpha", "two")

    summary = daemon.boards_seen_summary(ops)
    assert summary == {"seen_through": 0, "unread": 2}


async def test_mark_seen_lowers_unread_and_a_new_post_reraises_it(daemon):
    """The badge goes quiet on acknowledgement and comes back on the next post."""
    daemon.kernel.create_board("ops", created_by="operator")
    ops = board(daemon)
    first = ops.post("alpha", "one")

    summary = daemon.mark_board_seen(ops.oid, first["id"])
    assert summary == {"seen_through": first["id"], "unread": 0}

    second = ops.post("alpha", "two")
    summary = daemon.boards_seen_summary(ops)
    # Ids are the board's own counter, so the new post is strictly newer.
    assert second["id"] > first["id"]
    assert summary == {"seen_through": first["id"], "unread": 1}


async def test_cursor_never_lowers(daemon):
    """Confirming through an older id cannot unsee what was already confirmed."""
    daemon.kernel.create_board("ops", created_by="operator")
    ops = board(daemon)
    first = ops.post("alpha", "one")
    second = ops.post("alpha", "two")

    daemon.mark_board_seen(ops.oid, second["id"])
    summary = daemon.mark_board_seen(ops.oid, first["id"])

    assert summary["seen_through"] == second["id"]
    assert summary["unread"] == 0


async def test_unknown_oid_is_a_clean_miss(daemon):
    """No board with that oid: KeyError, which the web layer turns into a 404."""
    with pytest.raises(KeyError):
        daemon.mark_board_seen(9999, 1)
