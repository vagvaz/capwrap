# capwrap

Capability-governed sandboxes for AI coding agents: bubblewrap isolation, a
capability proxy, and per-role rights, so agents can work autonomously without
unbounded trust.

## Language

### Trust and permissions

**Quiet by default**:
The target posture: ordinary work rarely prompts, reached through complete
explicit allowlists — permissions stay deny-by-default; a prompt means a list
is missing an entry.
_Avoid_: removing allowlists, yolo mode

**Boundary**:
A container-level limit enforced by bubblewrap or the capability proxy:
network, child containers, writes outside the worktree, host mounts.

**Escalation**:
An agent request for a capability grant, needed to cross a boundary. Produces
an approval card.
_Avoid_: prompt, ask

**Capability grant**:
An operator-approved addition of a capability to a running container, granted
live when the mechanism allows (network rules, child-spawn authority);
structural limits (worktree bind) are refused with "respawn required".
_Avoid_: live grant, permission change

**Approval**:
A permission request from an agent, answered allow / reject / grant / explain.
Allow decides once; grant approves and persists; explain replies with text
instead of deciding.
_Avoid_: question (that is conversation, not permission)

**Always-allow**:
A persisted approval (the card's grant action); future identical requests
skip the operator.

**Question**:
An agent-to-operator conversation turn. May carry options or free text.
Never a permission decision.
_Avoid_: approval, ask (as a noun for permissions)

### Teams

**Team**:
A named set of containers with complementary roles, a shared goal, peer
messaging granted among members, and one shared board.
_Avoid_: group, squad

**Goal**:
The stated objective a team is created to reach, recorded at creation and
visible to every member.

**Success criteria**:
The team's stated definition of done, recorded at creation alongside the
goal.

**Autonomous mode**:
A per-container mode where the agent proceeds without blocking on the
operator: questions are answered with "use best judgment, note it" and
recorded for later review. Approvals are never auto-answered — only harness
questions.
_Avoid_: headless, yolo mode

**Question routing**:
The per-container choice of where agent questions surface: **forward** (the
operator's console), **block** (the harness's own TUI — operator comes to
the agent), or **auto** (autonomous mode).
_Avoid_: mode (alone — it is one toggle with three positions)

**Project**:
A named, predefined configuration for where agents work: the source repo,
the base branch, extra mounts, host env vars, and a routing default.
_Avoid_: template, workspace
