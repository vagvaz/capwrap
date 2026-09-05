"""Kernel objects -- the things capabilities point at.

Objects live in one flat table owned by the kernel and are addressed by an
integer `oid` that **never leaves the kernel**.  Containers only ever see slot
numbers in their own capability table, so an agent cannot name an object it has
not been given, cannot enumerate objects it does not hold, and cannot forge a
reference by guessing.  That property is the whole reason for the indirection;
it is what makes the system analysable rather than merely locked down.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .rights import Rights

ObjectKind = Literal[
    "container", "dataspace", "factory", "gate", "net_rule", "board"
]

_next_oid = itertools.count(1)


@dataclass
class KernelObject:
    """Base for everything a capability can refer to."""

    oid: int
    kind: ObjectKind
    label: str

    def describe(self) -> dict[str, Any]:
        return {"kind": self.kind, "label": self.label}


@dataclass
class ContainerObject(KernelObject):
    """A sandbox, running or not.

    The object outlives the process: killing a container leaves the object (and
    everyone's capabilities on it) in place, marked dead, so that revocation and
    audit stay meaningful and a restart can reuse the same identity.
    """

    kind: ObjectKind = field(default="container", init=False)
    name: str = ""
    parent: str | None = None
    #: Ed25519 public key, hex. What a reader checks a signed board post
    #: against; the matching seed only ever exists inside the container.
    public_key: str = ""
    state: str = "created"
    pid: int | None = None
    exit_code: int | None = None
    #: Mount summary, for the web UI.
    mounts: list[str] = field(default_factory=list)

    def describe(self) -> dict[str, Any]:
        return {
            **super().describe(),
            "name": self.name,
            "parent": self.parent,
            "public_key": self.public_key,
            "state": self.state,
            "pid": self.pid,
            "exit_code": self.exit_code,
        }

    @property
    def alive(self) -> bool:
        return self.state == "running"


@dataclass
class DataspaceObject(KernelObject):
    """A host path that may be shown or given to a container."""

    kind: ObjectKind = field(default="dataspace", init=False)
    path: Path = Path("/")
    ds_kind: Literal["dir", "file", "git_repo"] = "dir"

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "path": str(self.path), "ds_kind": self.ds_kind}


@dataclass
class FactoryObject(KernelObject):
    """Authority to create containers, and the budget for doing so.

    Quota is consumed on creation and *not* returned when a child dies: a
    factory's allowance bounds how many containers may ever be spawned through
    it, which is what stops a runaway agent from cycling containers forever.
    """

    kind: ObjectKind = field(default="factory", init=False)
    quota_containers: int = 0
    used_containers: int = 0
    #: What the *spawner* receives on each container it creates through this
    #: factory. Without it a parent can create a child and then have no way to
    #: reach it at all, which makes a supervisor agent impossible to express.
    child_rights: Rights = Rights.NONE

    @property
    def remaining(self) -> int:
        return max(0, self.quota_containers - self.used_containers)

    def describe(self) -> dict[str, Any]:
        return {
            **super().describe(),
            "quota_containers": self.quota_containers,
            "used_containers": self.used_containers,
            "remaining": self.remaining,
            "child_rights": self.child_rights.names(),
        }


@dataclass
class GateObject(KernelObject):
    """A bare message endpoint, not tied to a container's lifetime.

    Used for reply channels and for the operator's own inbox, so a container can
    be given the right to talk to *something* without being given a capability
    on the container behind it.
    """

    kind: ObjectKind = field(default="gate", init=False)
    owner: str = ""

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "owner": self.owner}


#: How many posts a board keeps. Old ones fall off the end: a board is a place
#: agents coordinate through, not a durable log -- the audit log is that.
BOARD_HISTORY = 500


@dataclass
class BoardObject(KernelObject):
    """A shared message board several containers can read and write.

    Different from a mailbox in the way that matters for coordination: a mailbox
    is one queue with one owner and reading *consumes*, so two agents cannot both
    see the same message. A board is read without taking anything away, so every
    holder sees the whole conversation, and a reader that joins late catches up
    by reading from the start.

    Posting and reading are separate rights, which is the point of putting it
    behind a capability at all: an orchestrator can give a worker `send` so it
    reports progress without being able to read its peers' notes, or `read` so it
    follows along without being able to speak.
    """

    kind: ObjectKind = field(default="board", init=False)
    #: What the board is for, as the creator named it.
    topic: str = ""
    #: Who may see it exists at all. Purely informational; rights decide access.
    created_by: str = ""
    posts: list[dict[str, Any]] = field(default_factory=list)
    _next_post: int = 1

    def post(
        self, sender: str, payload: Any, signature: str = "", key: str = ""
    ) -> dict[str, Any]:
        import time

        entry = {
            "id": self._next_post,
            "from": sender,
            "payload": payload,
            "ts": time.time(),
            # Carried with the post so a reader can check it later without
            # asking the daemon who wrote it -- which is the whole point.
            "signature": signature,
            "public_key": key,
            "signed": bool(signature),
        }
        self._next_post += 1
        self.posts.append(entry)
        if len(self.posts) > BOARD_HISTORY:
            del self.posts[: len(self.posts) - BOARD_HISTORY]
        return entry

    def read(self, since: int = 0, limit: int = 50) -> list[dict[str, Any]]:
        """Posts after `since`, oldest first.

        Non-destructive, and every reader keeps its own `since` -- which is what
        lets several agents follow the same board without racing each other for
        messages.
        """
        newer = [p for p in self.posts if p["id"] > since]
        return newer[: max(1, limit)]

    def describe(self) -> dict[str, Any]:
        return {
            **super().describe(),
            "topic": self.topic,
            "created_by": self.created_by,
            "posts": len(self.posts),
            "latest": self.posts[-1]["id"] if self.posts else 0,
        }


@dataclass
class NetRuleObject(KernelObject):
    """Permission to reach one set of network destinations.

    One rule, one object, one capability -- rather than a single "network"
    capability carrying a list of patterns. That is what makes narrowing work:
    giving a child access to the docs site but not the package registry is an
    ordinary delegation of one of the two capabilities you hold, checked by the
    same mapping database as everything else. A capability whose *contents* had
    to shrink would need regex containment, which is undecidable in general and
    would put a guess at the centre of the security model.

    The pattern is matched against ``host:port``. Nothing here inspects a URL
    path: for HTTPS the proxy only ever sees the CONNECT target, and a rule that
    claimed otherwise would be describing authority it cannot enforce.
    """

    kind: ObjectKind = field(default="net_rule", init=False)
    #: Short name the operator and the agent refer to it by, e.g. "pypi".
    rule: str = ""
    #: Anchored regex over ``host:port``.
    pattern: str = ""

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "rule": self.rule, "pattern": self.pattern}


def new_oid() -> int:
    return next(_next_oid)


@dataclass
class CapRef:
    """One entry in a container's capability table.

    `node` links back into the mapping database, which is what makes recursive
    revocation possible: revoking a mapping walks its subtree and removes the
    `CapRef` each descendant is holding.
    """

    oid: int
    rights: Rights
    node: int
    #: Human-facing name, shown by `capctl caps`.  Purely cosmetic.
    label: str = ""

    def allows(self, needed: Rights) -> bool:
        return needed in self.rights
