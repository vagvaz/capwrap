# Plan: console mailbox visibility, consumption controls, and spawn-from-UI

Hours of grilling: 3 rounds. Grounded in live use — an agent sat on two unread
operator messages with no indication anywhere, and only saw them when the
operator typed "check your mailbox" directly into its terminal.

## Decided — mailbox

### Unread semantics
- A container's badge counts **pending-recv**: queued messages not yet dequeued
  by the agent's own `capctl recv`.
- The operator reading mailbox history (console UI, `/api/containers/{name}`,
  `/shared/inbox` files) never clears it. Delivery/trace/history/file reads are
  **not** consumption — no claim the agent understood anything.
- Mailbox is `ipc/mailbox.py`: `pending` = `queue.qsize()`, `recent(l)` =
  bounded history deque. Dequeuing (`receive`) leaves history intact — history
  can show delivered-vs-dequeued state without touching the queue.

### Badge surfaces
- **Container list** (`/api/overview` payload + tree): per-container pending
  count. The detail API already returns `mailbox`; the list payload needs the
  count added (not every message — a number).
- **Container detail**: pending count prominent; history rows show
  delivered vs dequeued and the dequeued timestamp where known.
- Web UI (`web/app.py`, `static/app.js`): badges re-render from `state`; push
  arrivals on the live event stream, refresh counts on the normal
  overview/refresh path rather than trusting events alone.

### Consumption control — nudge only
- NO operator queue-drain / pop endpoint. Viewing is nondestructive.
- A separate, explicitly labelled **"Nudge agent"** control:
  - types a fixed prompt (`Check your mailbox with capctl recv.`) plus Enter
    into the container's terminal;
  - confirmed before sending, with the terminal visible;
  - disabled while the container is stopped;
  - **never** fires automatically after a send;
  - does not change pending counts — only an actual `recv` does.
- Rationale (argued both sides before deciding): installing a
  kernel/journal-level consumption receipt would be a larger design; nudge
  matches the operator's observed workaround and leaves delivery in the
  agent's hands. Destructive clearing was rejected for now: it can silently
  remove messages the agent would have acted on.

---

## Decided — shared boards

- Unread = **"new since the operator last marked seen"** — NOT unread-by-agents.
- Daemon-side per-board cursor, rendered into `/api/boards`:
  - keyed by **board ID / OID**, never topic text (duplicate topics are legal
    today, and `operator_grant` resolves by first topic match — don't add a
    second topic-keyed thing).
- Per-topic badge + total unseen retained posts (boards retain ~500 posts).
- **"Mark seen through #N"** control per board, acknowledging exactly up to
  and including the posts currently displayed. No background fetch.
- Cursor resets with daemon restart — boards are in-memory; consistent.
- Agent-side read receipts: deferred to a later round.

---

## Decided — spawn-from-UI

### Endpoints
- **POST /api/spawn** on the daemon's FastAPI app. Runs *compose.py's logic
  imported* (no shell-out) and registers through the operator `up` path
  (`daemon.register` → `link_all_peers` → optionally start). **Not** the kernel
  factory — that's for agent-created children; this is an operator authority.
- **GET /api/caps/tables** (new): live-serialized role / persona / agent tables
  from the same in-file `ROLES` / persona-glob / `AGENT_SETUP` data compose.py
  reads, so the dialog lists what exists without hard-coding, and so role,
  preview, and spawn can't drift. Summary text + flags (worktree, network,
  factory quota) included.

### Dialog (web UI)
- Role × persona × agent pickers, populated from /api/caps/tables (written
  fresh, distinct from the existing `PANEL_EXTRA` / layout code so it can't
  collide with panel dock state).
- Worktree ("work") roles require explicit **repo + base** — defaults offered
  are compose.py's current hard-codes (`~/capwrap-demo/repo`, `main`), but the
  fields are real inputs, not invisible.
- **Read-only preview** of generated TOML, the combined role+persona prompt,
  resolved name, branch, argv, and generated output paths before launch.
- Collisions: proposed suffix (base-2, base-3…) caret-suggested in the name
  field; operator can edit or revert. Re-preview is required after any change.

### Collision policy
- **409 `preview_stale`** when the previewed digest (generated config bytes +
  prompt bytes + resolved base commit + normalized inputs) changed between
  preview and launch — client re-previews.
- Refuse with clear errors on: existing container name (kernel `tasks` check,
  matching `POST /api/containers` behavior), branch already checked out or
  existing in the repo, retained state directory, pre-existing generated
  output path, or dangling symlinks/same-inode outputs. No silent rename, no
  importing/quarantining worktrees, no overwriting the shared house.md from a
  stale copy.
- Preview generation itself is side-effect-free (no writes to `built/` during
  the dialog; materialization happens under the launch lock at submit time).

### Composition packaging
- Extract the pure generator (template + data structures) into a small module
  importable by both compose.py (CLI keeps its file-writing behavior) and the
  app's /api/spawn + /api/caps/tables. Move the generated-*content* (roles,
  personas, house.md, template) into the package so the daemon can build
  configs even when the CWD isn't the checked-out repo. The example dir keeps
  working as a thin CLI wrapper around the importable module.
- Agent profiles from compose.py's `AGENT_SETUP` drive approved-executable
  argv declaration, so preview and launch stay consistent.

### Concurrency / restart
- Since worktree reuse exists in the runtime today (and is *deliberate* for
  restart), new spawns under the UI lock against the daemon's existing
  single-registration lock: two simultaneous identical submits cannot both
  see "free" and half-register.
- One daemon owns one `CAPWRAP_STATE`; a second daemon instance against the
  same state dir is told clearly at startup instead of colliding on shared
  per-name paths and stale sockets mid-flight. This is a whole-system
  restriction, documented, not something the web app can work around.

### Failure on launch
- Revoke granted authority, close sockets/PTY as today, but **retain**
  generated files, retained state, and branch/worktree for diagnosis.
- Report failed phase, retained paths, and whether cleanup fully completed.
- Same-name retry stays blocked until either the operator explicitly dismisses
  the stuck registration (a small helpful console link, not a silent
  overwrite) or the container name gets a suffix via the dialog.
- A process that starts and immediately exits is shown as **exited**, not
  claimed "ready".

---

## Deferred (deliberately out of scope for this plan)
- Long-lived durable message/board storage (history already capped ~500;
  queues unbounded pending).
- Agent-by-agent read receipts on boards.
- Rewriting the events transport for guaranteed delivery (event subscribers
  have bounded queues; UI refreshes counts from `/api/overview`, and the plan
  avoids treating events as authoritative store).
- Changing `MAIL/mailbox` semantics for the CLI (`capctl recv` semantics are
  untouched; UI is additive).
- An automatic readiness probe after launch.

## Open questions for implementation phase
- Whether the pending count should be a top-level `/api/overview` field per
  container, or piggybacked on the existing tree payload — implementation
  detail; the plan only requires it be present in list payloads.
- Whether pushing arrivals for *line-level* read/unread state on boards is a
  follow-up. Approved scope is badge + mark-seen only.
