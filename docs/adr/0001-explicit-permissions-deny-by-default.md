# Explicit permissions, deny by default: complete the allowlists, don't flip them

The original role configs enumerated allowed tools as agent permission rules,
and agents prompted the operator for every un-enumerated command — dozens of
interruptions per real session. An earlier draft of this decision proposed
flipping to deny-list semantics ("quiet by default": agents run free, only
boundary crossings escalate). The operator rejected the flip: capwrap is a
permission system, and its permission lists should stay explicit and
default-deny — prompting on the un-listed is the enforcement boundary, not
noise to be engineered away. The storm's actual cause was incomplete lists:
`capctl` commands, ordinary dev commands (make, pytest, node) and read-only
shell were never enumerated. So the fix is content, not semantics: a complete
ambient baseline (communication fabric, read-only shell, git read) plus
per-role work commands, with role lists remaining the explicit grant of what
that role may do. What the grill did settle and stands: a daemon-side grant
table so "always allow" persists; escalation to live capability grants for
boundaries (network, child-spawn); approvals (allow / reject / grant /
explain) kept separate from questions; per-container question routing
(forward / block / auto), with approvals never auto-answered.

## Considered options

- **Flip to deny-list semantics** (agents free inside, prompts only on
  boundary escalation): rejected by the operator — it removes the explicit
  permission surface that is the point of the tool, and makes the agent-side
  layer a duplicate of the container boundary instead of an enforcement
  layer.
- **Auto-answer prompts**: rejected — it hides the noise instead of fixing
  its cause, and auto-answering permission requests is the trust capwrap
  exists to avoid.

## Consequences

- Role configs keep `allow`/`deny` semantics; the ambient baseline becomes
  part of every role's explicit grant, visible in the generated TOML.
- Completing the lists is ongoing maintenance: a prompt in real work is a
  signal that a list is missing an entry, and the fix is to add it (or
  always-allow it via the grant table), not to widen the default.
- The agent-side layer remains a second enforcement surface alongside the
  container boundary; the two must agree, and the role docs say so.
