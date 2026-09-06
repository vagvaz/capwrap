# Quiet by default: deny lists and capability grants, not allowlist prompting

The original role configs enumerated allowed tools as agent permission rules
(Claude's native format), so agents prompted the operator for every
un-enumerated command — dozens of interruptions per real session, while the
container boundary (bubblewrap, capability proxy) already enforced the actual
security model. We decided the agent-side permission layer stops duplicating
enforcement: agents run free inside the sandbox; role configs carry a deny
list plus boundaries; crossing a boundary is an explicit escalation that
produces an approval card (allow / reject / grant / explain), and an approved
grant applies live where the mechanism allows. Approvals are never
auto-answered; only harness questions can be (forward / block / auto routing).

## Considered options

- **Broaden the allowlists** (bigger baselines per role): rejected — prompts
  shrink but never disappear, and every new tool a role needs becomes another
  config edit. A treadmill, not a posture.
- **Keep allowlist semantics, auto-answer prompts**: rejected — it hides the
  noise instead of removing its cause, and auto-answering permission requests
  is exactly the trust capwrap exists to avoid.

## Consequences

- Enforcement of role guarantees (a reviewer cannot write) moves entirely to
  the container boundary, which already enforced it. Role configs and docs
  must state this explicitly, or the change reads as losing enforcement when
  it only stops duplicating it.
- The daemon gains a per-container grant table (persisted approvals) and a
  live-grant path for proxy network rules and child-spawn authority.
  Structural limits (worktree bind) answer "respawn required".
- The examples' role tables change meaning: `allow` lists become deny lists
  plus boundaries. Old configs keep working but express the wrong intent.
