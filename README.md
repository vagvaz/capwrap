# capwrap

Run several AI coding agents at once, on the same repo and the same data, without
them coordinating — and without giving any of them more authority than you meant to.

Two ideas, stacked:

**Filesystem isolation removes the need to coordinate.** Each agent gets an
*overlay* over shared data (it sees everything, its writes are private) or a
*git worktree* on its own branch (integration is an ordinary `git merge`). No
locking, no clobbering, no turn-taking.

**A capability system decides who may do what.** Modelled on Fiasco.OC/L4Re: a
host daemon holds all authority, each container gets only unforgeable local
capabilities, and rights can only ever *shrink* when delegated. One place to
revoke, and revocation is recursive.

On top of that, one web console: every agent's live terminal, the parent tree,
the capability graph, and a single approval queue so five agents don't mean five
windows to babysit.

---

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'

.venv/bin/capwrap doctor                # check the host can sandbox
examples/setup-demo.sh                  # build a playground repo + database

.venv/bin/capwrap up examples/agents/dev-a.toml examples/agents/dev-b.toml \
  --name "FastPath HashTable"
# → http://127.0.0.1:8420
```

`--name` is what the console and the browser tab are called. Several capwraps
run at once, one per piece of work, and without it every tab says "capwrap".

Two agents now share one repo and one database and cannot see each other's work:

```bash
ls ~/capwrap-demo/db                        # seed data, untouched
git -C ~/capwrap-demo/repo branch           # capwrap/dev-a, capwrap/dev-b, main
```

---

## Host setup

capwrap needs `bubblewrap`. Via nix:

```nix
# ~/.config/home-manager/home.nix
home.packages = with pkgs; [ bubblewrap fuse-overlayfs git ];
```

**capwrap does not need an AppArmor profile installed.** It used to, and the
reason is worth knowing. Ubuntu sets
`kernel.apparmor_restrict_unprivileged_userns=1`, which forces any *unconfined*
program creating a user namespace into the `unprivileged_userns` profile — and
that profile denies capabilities inside the namespace, so bwrap fails at its
first step:

```
bwrap: setting up uid map: Permission denied
```

Ubuntu ships an exemption for bubblewrap, but it attaches **by path**, to
`/usr/bin/bwrap` only, so a nix-store bwrap is confined rather than exempted.

capwrap now tries *every* bwrap on the host and uses the first that can actually
build a namespace, preferring the packaged one. Nothing short of running them
tells you which is which, so it runs them. On a restricted host, `apt install
bubblewrap` is therefore the whole fix, and `doctor` says so:

```
[ok  ] bwrap can create namespaces: namespace + mounts OK (/usr/bin/bwrap), skipped 1 that could not
```

If you would rather keep using a bwrap outside `/usr`,
`scripts/install-apparmor-profile.sh` registers it — globbed so it survives
nixpkgs updates. That is now a preference, not a prerequisite.

```bash
sudo scripts/install-apparmor-profile.sh   # only if you want the nix one
```

A consequence worth knowing: the stacked profile denies capabilities to bwrap's
*children*, so **nested bwrap inside a sandbox cannot work**. Agents never create
sandboxes directly — they invoke a factory capability and the daemon does it.
That is the right design anyway.

---

## Container configs

```toml
name = "dev-a"

[runtime]
command = ["claude"]
cwd     = "/work"
approvals = "capwrap"                    # route tool prompts to the web console
auto_allow = ["Read", "Bash(git log*)"]  # things not worth waking a human for

[sandbox]
network = false                          # default
unshare = ["pid", "ipc", "uts", "cgroup"]

[[mounts]]                               # a repo → private branch + checkout
src    = "~/proj"
dest   = "/work"
mode   = "worktree"
branch = "capwrap/dev-a"

[[mounts]]                               # shared data → private writes
src  = "~/db"
dest = "/db"
mode = "overlay"

[[files]]                                # inject scaffolding
dest    = "/work/ROLE.md"
content = "You are agent A."

[caps]                                   # initial authority — this is all it gets
peers      = [{ container = "dev-b", rights = ["send", "inspect"] }]
dataspaces = [{ path = "~/ref", rights = ["read", "map", "delegate"] }]
factory    = { rights = ["create"], quota = { containers = 2 } }
```

### Mount modes

| mode | what the agent gets | writes go to |
|---|---|---|
| `ro` | read-only view | nowhere |
| `rw` | the real directory | the host, immediately |
| `tmpfs` | empty scratch | discarded with the container |
| `copy` | a private copy | its own copy |
| `overlay` | shared contents | a private upper dir |
| `worktree` | its own branch + checkout | its own branch |

`overlay` is right for data; `worktree` is right for a git repo, because you
integrate with `git merge` instead of diffing overlay upper directories by hand.
`share = "none"` uses `git clone --local` instead of a linked worktree when you
don't want agents sharing an object store.

### Nesting

Modes can be nested to any depth — a read-only tree with a writable overlay part
way down and a private copy below that:

```toml
[[mounts]]
src = "~/proj"; dest = "/a"; mode = "ro"          # visible, immutable
[[mounts]]
src = "~/proj/b"; dest = "/a/b"; mode = "overlay" # writes to a private upper
[[mounts]]
src = "~/proj/b/c"; dest = "/a/b/c"; mode = "copy"  # a private copy
```

Deeper mounts shadow shallower ones, so `/a` stays read-only while `/a/b` and
`/a/b/c` are writable, each into its own place. **Declaration order doesn't
matter**: bwrap applies mounts in argv order, so capwrap sorts them parents-first
before emitting. Without that, writing the child mount first would silently give
you a read-only `/a/b/c` with no error anywhere.

One constraint comes from bwrap: **the mountpoint must already exist in the
parent mount's source.** Mounting at `/a/b` when the host's `~/proj` has no `b/`
fails with `Can't mkdir: Read-only file system`, because bwrap cannot create a
directory inside a read-only bind. Create the directory on the host first, or
make the parent `rw`/`overlay` rather than `ro`.

---

## The capability model

Every container has a **capability table** mapping small integers — *slots* — to
objects. Agent A's slot 3 and agent B's slot 3 are unrelated, and neither can
name an object it wasn't given. There is no by-name variant of any operation, so
there is nothing to enumerate or guess.

```
$ capctl caps
SLOT  KIND       LABEL       RIGHTS
1     container  self        delegate,inspect,kill,read_output,send,signal
2     gate       operator    inspect,send
3     dataspace  /ref        delegate,map,read
4     container  peer:dev-b  inspect,send
```

**Rights only shrink.** Delegation goes through a mapping database that refuses
to pass on rights the delegator doesn't hold. This applies to spawning too: a
container created through a factory gets capabilities *derived from its
spawner's*, so a factory can never amplify authority.

**Revocation is recursive.** Revoking a capability kills its entire delegation
subtree — including things the recipient passed to agents you've never heard of.
Without that, "revoke" would be a lie after the first hop.

Agents use `capctl` inside the sandbox:

```bash
capctl caps                        # what am I allowed to do?
capctl send peer:dev-b "build ok"  # by label or by slot
capctl recv --wait                 # read my mailbox
capctl grant peer:dev-b 3 --rights read     # delegate, diminished
capctl revoke 3                    # take back everything I gave from slot 3
capctl map peer:dev-b 3 findings   # put a dataspace in their /shared
capctl spawn factory child.toml    # create a child, within quota
capctl screen peer:dev-b           # read its terminal (needs read_output)
capctl keys peer:dev-b down enter  # drive its TUI  (needs write_input)
capctl ask "may I install curl?"   # ask the human (a question, not a grant)
capctl request factory --quota 2   # ask for authority; approval grants it
```

### Identity

Each container's socket is bound at `/run/capwrap.sock` inside its sandbox and
at `<state>/containers/<name>/agent.sock` on the host. **The daemon knows who is
calling because it knows which socket accepted the connection.** No request
carries an identity, so there is no token to steal, forge or replay.

---

## Approvals

With `approvals = "capwrap"`, a Claude Code `PreToolUse` hook diverts every
permission prompt to the operator's queue in the web UI. The agent blocks inside
the hook until you answer, and your answer resolves the very future it is
waiting on — one process, one event loop, no polling.

`auto_allow` / `auto_deny` decide the boring cases locally, matching either a
bare tool name (`"Read"`) or `Tool(glob)` against its main argument
(`"Bash(git *)"`). The policy file is bound read-only, because an agent with a
shell would otherwise just widen its own permissions.

If the daemon is unreachable the hook returns `ask`, falling back to Claude's own
prompt. It never defaults to `allow` — that would silently disable every check.

### Setting Claude's permissions from the config

```toml
[runtime.permissions]
allow        = ["Read", "Bash(git status:*)", "Bash(git diff:*)"]
ask          = ["Write", "Edit"]
deny         = ["Bash(sudo *)", "Bash(curl *)", "WebFetch"]
default_mode = "default"        # plan | default | acceptEdits | bypassPermissions
```

This becomes the `permissions` block of the sandbox's `settings.json`, bound
read-only so the agent cannot rewrite its own rules.

**Why this needed a safeguard.** A container can spawn children. Nothing about a
factory capability says anything about *tool* permissions, so a container
confined to `Read` could spawn a child with `Bash(*)` and simply act through it
— the capability model would still be sound while the thing it protects walked
out the back.

So permission policies get the same treatment as rights: **they may only ever be
diminished.** A child's policy is checked against its parent's envelope before
the spawn, using a partial order over policies (`capwrap/kernel/policy.py`):

| child's change | result |
|---|---|
| exact copy | passes silently |
| drops an `allow` | passes silently |
| adds a `deny` | passes silently |
| moves a rule `allow` → `ask` | passes silently |
| makes a rule more specific (`Bash(git *)` → `Bash(git log *)`) | passes silently |
| allows a new tool | **operator approval** |
| broadens a glob (`Bash(git *)` → `Bash(*)`) | **operator approval** |
| replaces a glob with the bare tool (`Bash(git *)` → `Bash`) | **operator approval** |
| drops one of the parent's `deny` rules | **operator approval** |
| allows or asks for something the parent denies | **operator approval** |
| raises `default_mode` | **operator approval** |

A child that specifies no permissions inherits the parent's, which cannot
escalate by construction.

**Making it less human-invasive.** Two things keep the operator out of the loop
for the common cases. First, *narrowing is provably safe*, so it never prompts —
and most real changes are narrowing. Second, `permission_envelope` lets you
pre-authorise a range once, in the config, instead of approving each spawn:

```toml
[runtime.permissions]                 # what this container itself may do
allow = ["Read"]
deny  = ["Bash(sudo *)"]

[runtime.permission_envelope]         # the most it may ever give a child
allow = ["Read", "Bash(git *)"]
deny  = ["Bash(sudo *)"]
```

Now a child asking for `Bash(git status)` starts immediately, even though the
parent cannot run git itself; a child asking for `Bash(*)` still stops. The
envelope defaults to the container's own permissions, so the safe behaviour is
what you get without thinking about it.

Escalations that do reach you arrive in the same approval queue as everything
else, with the specific reason:

```
boss wants to spawn overreach with wider permissions than its own
  - allows Bash(*), which the parent does not allow
```

**Conservative by construction.** The pattern matcher returns "not covered"
whenever it cannot *prove* containment, so an exotic glob becomes a prompt
rather than a silent allow. Being wrong that way costs a click; being wrong the
other way costs the guarantee.

### Trying it

```bash
examples/setup-demo.sh
capwrap up examples/agents/claude-approval-demo.toml
# open http://127.0.0.1:8420
```

Claude is asked to create a file. `Read`/`Glob`/`Grep` are auto-allowed, so
within a few seconds exactly one thing reaches your queue:

```
claude-approve
Write: /work/hello.txt
{"tool": "Write", "input": {"file_path": "/work/hello.txt", "content": "hello"}}
[Allow] [Deny] [Open]
```

**Allow** → the file appears on branch `capwrap/claude-approve` and Claude
replies `DONE`. **Deny** → the file is never created and Claude tells you it was
blocked. Either way the decision is in the audit log next to the request.

## Environment variables, and endpoint credentials

`[runtime.env]` sets variables inline. For anything secret, use one of the two
sources that keep it out of the config file:

```toml
[runtime]
# Named variables lifted from the environment `capwrap up` was started with.
env_from_host = ["ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"]
# Or a KEY=VALUE file you keep out of the repo.
env_file = "endpoint.env"

[runtime.env]
ANTHROPIC_BASE_URL = "https://gateway.internal.example/v1"
```

Precedence is `[runtime.env]` > `env_file` > `env_from_host` > capwrap's
defaults. Only the variables you name cross the boundary; the daemon's own
environment does not leak in (set `sandbox.clear_env = false` if you actually
want it to).

For a custom endpoint with a bearer token, Claude Code reads `ANTHROPIC_BASE_URL`,
`ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_API_KEY` and `ANTHROPIC_CUSTOM_HEADERS`. See
`examples/agents/claude-custom-endpoint.toml` — with a token in the environment
the agent needs no copy of your host `~/.claude` credentials at all.

**Why not `--setenv`.** bwrap can set variables on its command line, and capwrap
used to. But `/proc/<pid>/cmdline` is world-readable:

```
$ ls -l /proc/<pid>/cmdline /proc/<pid>/environ
-r--r--r--  cmdline      ← every user on the host can read this
-r--------  environ      ← only the owner
```

so `--setenv ANTHROPIC_AUTH_TOKEN sk-ant-...` publishes the token to anyone with
a shell on the box. The environment is instead handed to bwrap as its own and
inherited by the agent, which puts it in `environ` where it belongs. A test
asserts that no environment value ever appears in argv, and
`capwrap run --dry-run` masks values whose names look like secrets.

## Prompts, roles, and a team of agents

A prompt reaches a container four ways:

| where | how | survives compaction? | best for |
|---|---|---|---|
| system prompt | `--append-system-prompt-file /prompts/role.md` in `runtime.command` | yes | who the agent *is* |
| project memory | a `CLAUDE.md` in the worktree, via `[[files]]` | yes, re-read | house rules |
| injected files | `[[files]]` inline or from `src`, or a `ro` mount | it is just a file | reference material |
| first message | `claude -p "..."` | no | the task, not the role |

Put the role in the **system prompt**. A role stated in the first user message is
one compaction away from being forgotten, and an agent can talk itself out of it.

`examples/team/` is a worked seven-role setup — orchestrator, explorer,
programmer, tester, reviewer, writer, architect — sharing one `prompts/`
directory mounted read-only into all of them:

```bash
capwrap up examples/team/*.toml
```

The point is that the roles are **enforced, not described**. The reviewer's
config denies `Write` and `Edit`, so it cannot edit whatever it is told; the
explorer is read-only; only the orchestrator holds a factory, and its
`child_rights` decide what it may do to the agents it spawns. See
`examples/team/README.md`.

### Crossing a role with a disposition

`examples/roles-and-personas/` separates the two, because they are not the same
kind of thing. A role is a job *and a set of capabilities*, and it is enforced.
A persona is a disposition -- a prompt, enforcing nothing.

```bash
examples/roles-and-personas/compose.py --list          # 22 roles x 15 personas
examples/roles-and-personas/compose.py architect:idealist architect:pragmatist
capwrap up examples/roles-and-personas/built/*.toml
```

Running one role twice with opposed dispositions is the useful case: each gets
its own branch, neither can see the other's work, so you get two independent
answers to the same question and the disagreement is the decision. Three roles
there have no repository at all -- `user`, `product-owner`, `domain-expert` --
because an opinion on whether the thing works is worth something precisely when
it was not formed by reading the source.

## Agents discover capctl on their own

Every container gets a Claude Code skill at
`$HOME/.claude/skills/capwrap/SKILL.md` describing the capability model and the
`capctl` commands, so an agent finds out how to message a peer or ask you a
question without it being repeated in every prompt. Turn it off with
`[runtime] capctl_skill = false`.

## Asking for a capability, vs. asking a question

`capctl ask` is a *question*. Approving it tells the agent "yes" and performs
nothing — so an agent that asks "may I have a factory?" and is told **allow**
still has an unchanged capability table. That is confusing enough that agents
report it as a bug.

`capctl request` closes the loop: the approval **is** the grant.

```bash
capctl request factory --quota 2 --reason "need a helper to run the test suite"
capctl request container dev-b --rights send,inspect --reason "report results"
capctl request dataspace /srv/data --rights read
```

The operator gets a card with the reason and a rights picker showing the rights
that are meaningful for that object kind, and **Grant** performs the delegation
atomically. The agent's blocked call returns the new slot, usable immediately:

```
granted: slot 3 "factory" with create
```

The operator can hand back **less** than was asked for — tick fewer rights and
that is what gets delegated. Denying grants nothing. Everything granted this way
is an ordinary mapping-database entry, so it is audited and recursively
revocable like any other.

Naming a container in a request is not a hole in "no ambient authority": the
agent still cannot *act* on anything it holds no slot for. It is asking a human,
and the human resolves the name and decides. A request is not a reference.

`examples/agents/request-demo.toml` shows both side by side.

## Granting authority while things are running

Capabilities are not frozen at spawn. In the **Capabilities** tab, select a
container and use **Grant a capability…** to hand it authority over another
container, picking the exact rights. The operator holds a root capability on
everything, so this is an ordinary delegation through the kernel — audited like
any other, and revocable (recursively) from the same table.

The same thing over HTTP:

```bash
curl -X POST localhost:8420/api/caps/grant -H 'Content-Type: application/json' \
  -d '{"holder":"dev-a","target_container":"dev-b","rights":["send","inspect","kill"]}'
# -> {"holder":"dev-a","slot":5,"rights":[...],"label":"peer:dev-b#2"}
```

A second grant on the same container gets a distinct label (`peer:dev-b#2`), so
`capctl kill peer:dev-b#2` stays unambiguous.

## What a parent gets from its children

Spawning is one-directional by default: the child receives a `parent` capability
pointing back, and that used to be all — the parent got **nothing** on the child
it had just created, so a supervisor agent could not message, watch or stop its
own children without asking you for a capability first.

A factory now says what the spawner receives:

```toml
[caps.factory]
rights       = ["create"]
quota        = { containers = 3 }
child_rights = ["send", "inspect", "read_output", "write_input"]
```

Each spawn hands the parent a slot labelled `child:<name>`:

```
$ capctl spawn factory /shared/child.json
spawned helper (2 left in the factory)
  you hold it in slot 4 as child:helper with inspect,read_output,send,write_input

$ capctl send   child:helper "start with the parser tests"
$ capctl screen child:helper
$ capctl keys   child:helper down enter
```

The default is `["send", "inspect"]` — enough to talk to a child and see whether
it is alive. Reading its screen, typing at it and killing it are larger grants
and have to be named. `child_rights = []` restores the old behaviour: a factory
that creates containers its owner cannot reach.

The ceiling is set by whoever wrote the parent's config, not by the parent.

### Factories cannot be amplified through a child

A container with a quota of 1 could otherwise create a child with a quota of 100
and spawn through that instead — the allowance multiplied by one level of
indirection. A spawned factory is now clamped to its parent's **remaining**
quota, and may not grant stronger `child_rights` over grandchildren than the
factory it came from:

```
cannot give kid's factory kill over its children: this factory only grants
inspect|send
```

Note the consequence: a parent with a quota of 1 that spends it creating a child
leaves that child a quota of 0. Give the parent a larger allowance if you want it
to delegate spawning.

## One agent driving another

A container holding `read_output` on a peer can read its terminal; with
`write_input` it can type at it. That is enough for a parent agent to drive a
child through an interactive prompt:

```bash
capctl screen peer:helper                 # what is on its screen now
capctl keys   peer:helper down down enter # arrows, Tab, Escape, ctrl-<key>, F1-F12
capctl type   peer:helper "yes" --enter   # text, optionally followed by Enter
```

`keys` is the part that matters for a tool prompt: a selection list is answered
with arrow keys and Enter, and neither is a character. Enter sends a **carriage
return**, which is what a terminal delivers in raw mode — a newline is often
ignored entirely by a full-screen TUI.

Reading returns the daemon's `pyte` screen model, not a byte log, so the caller
sees *what is currently displayed* — including which option is highlighted —
without needing a terminal emulator of its own.

They are two separate rights, and neither is implied by `inspect`. Knowing that
a container exists is a much smaller thing than reading everything on its
screen, which for an agent means its prompts, its file contents and whatever it
has been told.

## Dynamic mapping

`capctl map` hands a dataspace to another agent. *How* that lands depends on the
backend, and they differ in what "map" means:

| backend | mechanism | aliases? | privilege |
|---|---|---|---|
| `shared` | copy or symlink into the target's `/shared` | only if the source is already reachable inside | none |
| `nsmount` | bind mount into the running container's mount namespace | yes, fully | `CAP_SYS_ADMIN` on the host |

`nsmount` is the real thing: both containers look at the same filesystem
objects, a write on one side is visible on the other immediately, and a
directory can be mapped as a directory. It works by detaching the source with
`open_tree(OPEN_TREE_CLONE)` on the host, then joining the container's user and
mount namespaces and reattaching it with `move_mount`.

Two details that are easy to get wrong, both learned the hard way:

- The host path must be captured **before** the namespace switch. Once you are
  in the container's mount namespace the host filesystem is unreachable by path,
  because bwrap pivot_root'd away from it. A detached mount fd survives; a path
  does not.
- The user and mount namespaces must be joined in **one** `setns` call with a
  pidfd. Joining them separately fails with `EPERM`, because the mount namespace
  check is made against the user namespace in the pending credential set.

**Which one you get.** `capwrap doctor` reports it, and decides by *doing* it —
attempting a real mount into a throwaway container — because the failure is a
capability check deep in the kernel that nothing observable from outside
predicts:

```
  [warn] live remapping (nsmount): CAP_SYS_ADMIN is required on the host
  mapping backend: shared
```

An unprivileged daemon can join a container's namespaces and even reads back a
full capability set there, but `open_tree` and `mount` still return EPERM. To
get `nsmount`, run the daemon with the capability — e.g. a systemd unit with
`AmbientCapabilities=CAP_SYS_ADMIN` — and the same code path succeeds. Force a
backend with `CAPWRAP_MAPPING_BACKEND=shared|nsmount|auto`; naming one that is
unusable is an error rather than a silent downgrade, so you are never told a
mapping is an alias when it is a copy.

`mode="copy"` always copies, on either backend: there is no aliasing to
preserve, and a copy survives the container restarting where a mount does not.

## Killing a container cleanly

`capwrap stop`, the web UI's **Stop**, `capctl kill`, and a daemon crash all end
with **nothing left running**. The guarantee comes from the PID namespace rather
than from signalling: bwrap is pid 1 inside it, and when pid 1 exits the kernel
SIGKILLs every remaining process in the namespace. That covers processes which
`setsid` themselves out of the container's process group, which plain
`kill(-pgid)` would miss.

Because the guarantee depends on it, `sandbox.unshare` must include `pid`; a
config that leaves it out is rejected rather than silently leaking processes.
`--die-with-parent` extends the same property to a daemon crash — `kill -9` on
the daemon tears the namespace down too. Both cases are covered by tests.

## Dismissing a finished container

A stopped container stays in the tree on purpose — you usually want to read its
exit code and see where it sat. When you don't, hover it in the container list
and click **×**, or use **Dismiss N finished** to clear them all at once.

Dismissing does three things that have to happen together:

- revokes everything it held, recursively;
- revokes every capability **others hold on it** — otherwise a peer keeps a slot
  pointing at an object that no longer exists, which `capctl` would quietly skip
  while the slot stayed occupied;
- reparents its children to its own parent — the tree is walked down from the
  roots, so a child whose parent vanished would disappear from the UI while
  still running.

A **running** container is refused; stop it first. Its work on disk is kept —
the git branch, overlay writes and private copies all survive. To delete those
too:

```bash
capwrap clean <name> --yes
```

## If the source repo is recreated

A container's state outlives its repo. Re-clone or recreate the repo and the
checkout under the container's state directory still points at an admin
directory that no longer exists — the agent then meets
`fatal: not a git repository` with nothing explaining why.

capwrap detects that at startup, renames the stale checkout to
`work.orphaned-<timestamp>` and cuts a fresh worktree, printing what it did. It
renames rather than deletes, because the old checkout may hold work the agent
never committed.

---

## Console layout

The container list and the approvals inbox are panels, and each can be moved to
any edge of the window with the arrows in its title bar — left, right, top or
bottom, including both on the same edge, where a swap control appears to reorder
them. Drag the splitters to resize, double-click one to reset, or focus it and
use the arrow keys (Shift for bigger steps). Where the panels sit and how big
they are persists in `localStorage`; `⊞` in the header puts everything back.

The terminal reserves a lane for its scrollbar rather than letting xterm lay a
column out underneath it, which is what used to paint over the right-hand border
of a full-screen TUI.

### Messages

An opt-in tab recording every message the kernel delivers between containers,
payloads included, filterable by sender and recipient. Off by default and cleared
when switched off: the audit log already records *that* a message was sent, and
by whom; this records what was in it, which is the agents' working content rather
than metadata. `capwrap up --trace` starts with it on.

## Networking

Network access is a capability. A container reaches the destinations it holds a
rule for, and nothing else:

```toml
[[caps.network]]
name    = "pypi"
pattern = '(pypi\.org|files\.pythonhosted\.org):443'

[[caps.network]]
name    = "anthropic"
pattern = 'api\.anthropic\.com:443'
```

A pattern is a regex over `host:port`, anchored at both ends before it is used —
otherwise `pypi\.org:443` would also match `pypi.org:443.attacker.example`. Ports
count: `example.com:443` does not grant `example.com:22`.

The container keeps `--unshare-net`, so it has no route anywhere. Its only way
out is a unix socket bind-mounted into the sandbox, with a small relay inside
turning that into an ordinary `HTTP_PROXY` every tool understands. The daemon
answers each request by asking the kernel, and audits both outcomes:

```
ALLOW netty  example.com:443   {"rule": "example"}
DENY  netty  pypi.org:443      {"held_rules": ["example", "anthropic-docs"]}
DENY  netty  example.com:8443  {"held_rules": ["example", "anthropic-docs"]}
```

A denial is an HTTP 403 that names the rules the agent does hold and how to ask
for another, so it can act on the refusal instead of hunting for a network fault:

```bash
capctl net                    # what may I reach, and through which rule?
capctl request net_rule 'pypi=(pypi\.org|files\.pythonhosted\.org):443' \
  --reason 'pip install needs the package index'
```

Approving that performs the delegation and the rule is live for the next
connection. Revoking it in the console closes the hole just as immediately, and
recursively.

Each rule is its own object and its own capability, which is what makes narrowing
work: giving a child the docs rule but not the registry rule is an ordinary
delegation. The alternative — one capability holding a list of patterns, narrowed
on delegation — would need to decide whether one regex contains another, which is
undecidable in general.

Only `host:port` is ever inspected. The proxy does not terminate TLS, so it
cannot express path rules over HTTPS — and cannot read the agent's traffic
either, which is the other half of that trade.

### The escape hatch

`sandbox.network = true` shares the host's whole network namespace, and is
refused alongside any rule list, because no proxy can constrain a container that
has its own route out.

It also still defeats the capability model: `127.0.0.1` in such a container is
the host's loopback, so the agent can reach the console — which has no
authentication — and grant itself capabilities.

```
$ curl -X POST http://127.0.0.1:8420/api/caps/grant -d '{"holder":"me", ...}'
{"slot":4,"rights":["inspect","kill","send","write_input"]}
```

Verified. Treat a `network = true` container as fully trusted, or use rules
instead. `docs/networking.md` has the design and what was considered against it.

## Explaining a request

An approval card says `Bash: curl -fsSL https://get.example.sh | sh`, which is
the information an operator has least use for at the moment they have to decide.
The question is not what the command *is* but what it will *do*, and whether it
is a reasonable thing for this agent to be asking right now.

The **Explain** button on each card asks Claude exactly that, and shows the
answer beside the request:

```bash
pip install 'capwrap[explain]'          # optional; nothing else needs it
export ANTHROPIC_API_KEY=...            # or `ant auth login`
```

The request reaches the model fenced as data, with the container's own
configuration for context, and the system prompt says its job is to describe and
flag rather than to recommend. That matters because the thing being explained was
written by an agent that may be trying to get a dangerous action approved by
describing it reassuringly. The answer is shown as advice about untrusted input,
not as a verdict — it decides nothing, and the operator still clicks the button.

`CAPWRAP_EXPLAIN_MODEL` overrides the model.

## The terminal console

```bash
capwrap tui --port 8420
```

The same job as the web console, for when you are already on the box over SSH.
It talks to a running instance over HTTP, so it works against one somebody else
started.

```
Tab     agents · approvals · boards · audit
j / k   move
Enter   attach to the selected agent   (Ctrl-] detaches)
y / n   allow / deny         g  go to the agent that asked
s x i   start · stop · interrupt
```

Answering approvals is the reason it exists: five agents blocked on prompts is
the problem capwrap is for, and needing a browser to unblock them puts a
graphical session in the middle of it. `g` on an AskUserQuestion goes straight to
that agent's terminal, which is the only place that kind of question can actually
be answered.

## Message boards

A mailbox is one queue with one owner, and reading it consumes: two agents
cannot both see the same message, and one that joins late has missed everything.
That is the right shape for handing work to somebody, and the wrong shape for
several agents coordinating.

A **board** is the other shape. An orchestrator — any container holding a factory
capability, since creating things for others to use is what a factory means —
sets one up and hands out as much of it as each worker needs:

```bash
# In the orchestrator:
capctl board create 3 standup          # 3 is its factory slot
capctl grant 4 6 --rights send,read    # alpha may post and read
capctl grant 5 6 --rights read         # beta may only read

# In alpha:
capctl board post standup "resize path reviewed"

# In beta:
capctl board read standup              # sees it; alpha still sees it too
capctl board read standup --since 2    # only what is new to me
```

Posting and reading are separate rights, which is the whole reason to put a
board behind a capability: a worker can report progress without reading its
peers' notes, or follow along without being able to speak. Reading takes nothing
off the board, so every holder sees the whole conversation and each keeps its own
`--since` cursor. Revoking the board from the orchestrator removes it from
everyone who got it from there, recursively, as with anything else.

The console's **Boards** tab shows every board, its posts, and — the part that
is not answerable from any one container's capability table — who may post to it
and who may only read.

A board keeps its last 500 posts. It is somewhere to coordinate, not a durable
log; the audit log is that.

### Sending to several at once

Separately, an ordinary message can go to more than one recipient in one call:

```bash
capctl send 3,4,7 "build is green"
capctl broadcast "build is green"      # everyone I may send to
```

Each slot is still checked and audited on its own, and one refusal does not
cancel the rest — a partial send is a real outcome and the caller is told which
recipients it missed. There is no "broadcast" right: this is exactly the messages
you could have sent individually, sent together. The console's composer uses the
same call when you tick more than one container.

## Signing

Every container gets an Ed25519 keypair when it is registered. The seed is
written into its own private directory and bind-mounted read-only into that
sandbox alone; the public key goes on the kernel object, where anyone can find
it. `capctl whoami` shows the fingerprint.

Board posts and messages can be signed:

```bash
capctl board post standup "resize path reviewed" --sign
capctl send 4 "bench is green" --sign
capctl broadcast "bench is green" --sign     # one signature, every recipient
```

and are verified on the way in — a signature that does not match is refused and
audited, never stored looking valid:

```
capctl: cap_error: that signature does not match the post; nothing was written
```

Reading checks them again, in the reader, rather than trusting a flag:

```
[1] from alpha [signed] (message): bench is green
#2 alpha [signed]: resize path reviewed
#3 beta [BAD SIGNATURE]: ...
```

### Why, when attribution is already unforgeable

capwrap knows who sent something because of *which socket it arrived on*, and no
agent can lie about that. So a signature is not how the kernel decides anything.
It buys three things the socket cannot:

- **It survives leaving capwrap.** An exported board, a log, a pasted transcript:
  a reader who was not there can still check the author.
- **It survives being forwarded.** A message signature covers the author and the
  payload, not the recipient, so an orchestrator relaying a worker's report
  cannot alter it on the way past and the eventual reader can tell. The kernel
  will correctly say the relay sent it; the signature still says who wrote it.
- **It does not require trusting the daemon.** The daemon recorded the post and
  could have edited it. The signature is checkable without taking its word.

What it does not buy: a message signature names no recipient, so someone holding
a capability on you could replay a message you sent them to a third party, still
signed. The claim is "this agent wrote this", not "this agent sent this to you".
Board posts additionally bind the board, so a post cannot be moved between them,
and the two kinds are domain-separated — a board signature will not verify as a
message signature.

Ed25519 is implemented in pure Python in `capwrap/guest/ed25519.py`, checked
against the RFC 8032 vectors, because `capctl` may not import from capwrap or
assume a package is installed and both sides need the identical code. It is
milliseconds per operation, which is fine for signing something somebody wrote
and would be unacceptable anywhere hot.

## Reaching the console from another machine

`capwrap up` binds `127.0.0.1:8420` by default. `--host` changes that:

```bash
capwrap up agents/*.toml --host 0.0.0.0            # every interface
capwrap up agents/*.toml --host 100.73.186.227     # a VPN address only
```

**The web interface has no authentication.** Anyone who can reach the port can
read every agent's terminal, type into it, spawn and destroy containers, and
grant capabilities — and a container that carries your Anthropic credentials
makes that a credential leak as well as a shell. `--host 0.0.0.0` on an untrusted
network hands all of that to the network.

Two safe ways to use it from a laptop, in order of preference:

```bash
# 1. Bind to a VPN address only. Nothing is exposed to the local network.
capwrap up agents/*.toml --host <your-tailscale-ip>

# 2. Keep it on loopback and tunnel in over SSH.
ssh -N -L 8420:127.0.0.1:8420 pi.local     # from the laptop; then use localhost:8420
```

Binding to anything other than loopback prints a warning saying as much.

## Commands

```
capwrap doctor                  check the host, with specific fixes
capwrap show <config>           validate a config and show what it resolves to
capwrap run <config>            prepare and enter one container, no daemon
capwrap run <config> --dry-run  print the bwrap command it would run
capwrap up <configs...>         run containers under the daemon + web UI
capwrap clean <name> --yes      delete a container's host-side state
capwrap state                   where state lives, and what's in it
```

---

## Layout

```
capwrap/
  config.py        TOML → validated ContainerConfig
  daemon.py        containers, PTYs, sockets; implements the kernel's effects
  kernel/          rights · captable · mapdb · objects · audit · policy · kernel
  runtime/         probe · fsprep · gitwt · bwrap · supervisor · mapper · nsmount
  ipc/             protocol · per-container socket server · mailboxes
  guest/           capctl, hook.py — the only things an agent sees
  web/             FastAPI + a vanilla-JS console, xterm.js vendored locally
tests/            193 tests; `-m sandbox` ones need a working bwrap
```

The split that matters: `kernel/` decides what is permitted and performs no I/O;
`daemon.py` performs the effects. So the whole permission model is testable
without a sandbox, a daemon or a filesystem — and every decision passes one
choke point, which is what makes the audit log trustworthy.

```bash
.venv/bin/pytest                  # everything
.venv/bin/pytest -m sandbox       # only the ones that really launch containers
```

---

## Status

Working: all mount modes, the capability kernel with recursive revocation,
per-container IPC, agent-to-agent messaging, dataspace mapping, factories with
quotas, the web console, and approval routing.

**Dynamic mapping** is implemented (`runtime/nsmount.py`, `runtime/mapper.py`)
and verified end to end, but only usable when the daemon holds `CAP_SYS_ADMIN`
on the host — see "Dynamic mapping" above. Unprivileged, it falls back to the
shared-directory backend, which cannot truly alias a directory.
