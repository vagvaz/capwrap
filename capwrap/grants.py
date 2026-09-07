"""The per-container grant table: "always allow" that persists.

A grant is an operator-approved addition to a container's permissions, recorded
so the same request never prompts again.  It is the daemon-side half of the
approval flow's "grant" action: approving a card with *grant* both answers the
waiting agent *and* appends the request to this table, so a future identical
request is auto-approved without troubling the operator.

The table is persisted to the container's state dir (``grants.json``, beside
``signing.key``) so it survives a daemon restart.  Matching reuses the policy
matcher (`capwrap.kernel.policy.Rule.covers`) -- the same vocabulary as role
allow rules -- so there is exactly one notion of "does this rule cover that
request" in the system.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from .kernel.policy import Rule


class GrantStore:
    """One container's grant table, persisted as JSON.

    Entries are ``{id, pattern, tool, created_at}``.  ``pattern`` uses the same
    vocabulary as role allow rules (e.g. ``Bash(git push:*)``); ``tool`` is
    optional and, when set, names the capability an escalation grant applies to
    (``network`` / ``spawn``) rather than a permission tool.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._grants: list[dict[str, Any]] = []
        self._load()

    # -- persistence -----------------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            # A corrupt grants file must not brick the container: treat it as
            # empty (deny-by-default) rather than crash the daemon.
            self._grants = []
            return
        entries = data.get("grants", []) if isinstance(data, dict) else []
        self._grants = [e for e in entries if isinstance(e, dict)]

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"grants": self._grants}, indent=2) + "\n")

    # -- mutation --------------------------------------------------------

    def add(self, pattern: str, tool: str | None = None) -> dict[str, Any]:
        """Append a grant and persist it.  Returns the stored entry."""
        grant = {
            "id": uuid.uuid4().hex,
            "pattern": pattern,
            "tool": tool,
            "created_at": time.time(),
        }
        self._grants.append(grant)
        self._save()
        return grant

    def remove(self, grant_id: str) -> bool:
        """Revoke a grant by id.  Returns whether one was actually removed."""
        before = len(self._grants)
        self._grants = [g for g in self._grants if g.get("id") != grant_id]
        if len(self._grants) != before:
            self._save()
            return True
        return False

    def list(self) -> list[dict[str, Any]]:
        return list(self._grants)

    # -- matching --------------------------------------------------------

    def matches(self, rule: Rule) -> bool:
        """Whether any permission grant covers `rule`.

        Only permission grants (those whose pattern parses as a tool rule) are
        considered; escalation grants (``tool`` in ``network``/``spawn``) are
        not matched against tool-permission requests.
        """
        for grant in self._grants:
            if grant.get("tool") in ("network", "spawn"):
                continue
            try:
                grant_rule = Rule.parse(grant["pattern"])
            except (KeyError, TypeError):
                continue
            if grant_rule.covers(rule):
                return True
        return False
