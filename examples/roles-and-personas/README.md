# Roles and personas

Twenty-two jobs, fifteen dispositions, and a script that crosses them.

```bash
examples/setup-demo.sh                  # or point the mounts at your own repo
export ANTHROPIC_AUTH_TOKEN=sk-ant-...

examples/roles-and-personas/compose.py --list
examples/roles-and-personas/compose.py architect:idealist architect:pragmatist
capwrap up examples/roles-and-personas/built/*.toml --name "shape of the storage layer"
```

## The distinction the whole example is about

A **role** is a job *and a set of capabilities*. It is enforced. A reviewer with
`Write` denied cannot write, whatever it concludes about itself halfway through
a session, and whatever an instruction buried in a file it reads tells it.

A **persona** is a disposition. It is a prompt and nothing else, and it enforces
nothing at all.

That asymmetry is easy to lose sight of once both are just markdown in the same
system prompt, so `compose.py` keeps them apart: the role table carries
permissions, mounts and capabilities, and the persona contributes exactly zero
of them. If you find yourself wanting a "careful" agent that genuinely cannot
break something, you want a role change, not a stronger adjective.

The composed prompt says so to the agent as well, and says the role wins when
the two pull against each other. A reviewer with a lazy disposition still
reviews; it just does not gold-plate the write-up.

## Roles

| | enforced by |
|---|---|
| architect, designer, api-owner | may `Write` notes, `Edit` denied |
| reviewer, security-reviewer, accessibility-reviewer, archaeologist | `Write` and `Edit` both denied |
| product-owner, domain-expert, user | no repository mounted at all |
| implementer, refactorer, debugger, performance-engineer, integrator, build-engineer, test-writer | a git worktree on their own branch |
| technical-writer | `Write` for docs, `Edit` denied |
| tester | may run anything, may change nothing |
| manager, tech-lead, orchestrator | hold a factory capability |

Three are worth singling out.

**user**, **product-owner** and **domain-expert** get no repository. That is the
point of them: an opinion about whether the thing works is worth something
*because* it is not formed by reading the source. A "user" that has read the
code is a developer with a hat on.

**security-reviewer** has `network = false`. Anything it claims, it worked out
from the code in front of it, and a finding it can only support by fetching
something is a finding it has not made yet.

**tester** may run arbitrary `Bash` but has `Write` and `Edit` denied. It can do
anything to a running system and nothing to the source, which is exactly the
authority the job needs and no more.

## Orchestrators that spawn orchestrators

`orchestrator` holds a factory and its prompt tells it to create a
sub-orchestrator when a piece has its own internal structure. The recursion is
bounded by the kernel rather than by the orchestrator behaving itself:

- a child's factory quota is capped at what **remains** in its parent's, so an
  orchestrator with three spawns left cannot mint a sub-orchestrator with ten;
- a child's `child_rights` must be **contained in** its parent's, so the rights
  listed in the top-level config are the ceiling for the entire tree beneath it.

Both are checked in `kernel/_grant_initial_caps`, which is why the prompt can
tell an orchestrator its budget is real: it is not being asked to restrain
itself, it is being told what it will find it cannot do.

## Personas

`idealist`, `pragmatist`, `devils-advocate`, `lazy`, `cpp-specialist`,
`newcomer`, `veteran`, `minimalist`, `firefighter`, `paranoid`, `empiricist`,
`distributed-systems-specialist`, `embedded-specialist`, `cost-conscious`,
`teacher`.

Each names its own failure mode, because a disposition applied without limit
stops being useful: the idealist that never ships, the devil's advocate that
only obstructs, the minimalist that deletes something load-bearing. An agent
told only its strengths will overplay them.

`lazy` is not a joke. A lazy implementer looks for the thing that already does
the job before writing anything, and deletes rather than adds. That is often the
correct instinct, and the prompt makes the argument on its own terms: half-done
work comes back to you, so being genuinely lazy means being thorough exactly
once.

## The head-to-head

The most useful thing here is running one role twice with opposed dispositions,
on the same task and the same repository:

```bash
examples/roles-and-personas/compose.py architect:idealist architect:pragmatist
capwrap up examples/roles-and-personas/built/architect-*.toml
```

Each gets its own git branch and neither can see the other's work, so there is
no anchoring and nothing to coordinate. You read two independent answers and
diff them. Where they agree, you can stop thinking; where they diverge is the
actual decision, stated twice from opposite ends.

`--all-personas reviewer` does the same across all fifteen, which is more than
anyone wants to read but makes the shape of the disagreement obvious.

## Adding your own

A persona is one markdown file in `personas/` and needs nothing else. A role is
a file in `roles/` plus an entry in the `ROLES` table in `compose.py`, and the
table entry is the part that matters -- it is what makes the role true rather
than merely described.

`built/` is generated. Edit the prompts, not the output.
