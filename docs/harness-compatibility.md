# Harness compatibility contract

What capwrap promises an agent harness, how that promise is verified, and the
state of each harness. Vocabulary is `CONTEXT.md`; the permission model is
`docs/adr/0001-explicit-permissions-deny-by-default.md`.

This document exists because the previous milestone shipped 41 commits with a
green 581-test suite while **three of four harnesses had a silently broken
path**. The suite was green because no test ever booted a harness, and in one
case because a test asserted the bug. Unit-green is not harness-correct.

---

## 1. The contract

A harness is **compatible** if and only if all four hold:

| # | Requirement |
|---|---|
| **(a) Loads** | The shim is provably loaded by the harness — evidenced by the harness's own load log or by executing the hook, never merely by "the config file was written". |
| **(b) Approvals block** | A permission request reaches the daemon, blocks, and resolves with the operator's decision (`allow` / `reject` / `explain`). |
| **(c) Questions route** | A question reaches the daemon and obeys the container's `question_routing` (`forward` / `block` / `auto`). |
| **(d) Failure is honest** | On daemon loss: an approval falls back to the harness's **native human prompt**, or **denies** if the harness has none. A question falls back to the native UI or a benign "unreachable" result. |

### The one rule behind (d)

> **Fail toward the human, never toward auto-allow.**

Auto-allow is never a failure mode. A degraded container may prompt a human or
refuse; it may never decide on the operator's behalf. This is ADR-0001's
deny-by-default posture applied to daemon availability.

### Questions are not approvals

`CONTEXT.md` draws the line semantically: an **Approval** is a permission
decision, a **Question** is a conversation turn. That split is sound and stays.

What it does *not* justify is leaving questions unrouted. Question routing is a
conversation feature — squarely the category the split says belongs to capwrap.
Any harness that answers a question without consulting `_route_question`
(`capwrap/daemon.py`) has a config lie: `routing = "forward"` is advertised and
inert.

Consequence for shims: a question must be classified explicitly
(`context["kind"] = "question"`, and no `tool` key) rather than inferred from
the *absence* of a tool name. Classification-by-absence is what caused a
conversation to be filed as an approval in the first place.

---

## 2. Failure posture, stated per harness

| harness | approval, daemon down | question, daemon down | note |
|---|---|---|---|
| claude | native prompt (`permissionDecision: ask`) | native picker | has a native prompt ⇒ may fall back to it |
| opencode v1 | native (permissions are native; no shim) | native question UI | v1's plugin API has tool hooks but no permission hooks |
| opencode2 | policy `fallback` decides — **never allow** | benign "unreachable" text | |
| pi | **deny** — `{block: true}` | n/a (no native question tool) | no native prompt exists ⇒ must refuse |
| generic | n/a — refuses `approvals = "capwrap"` | n/a | should also warn that routing is inert |

Divergence here is not sloppiness: it is one principle applied to what each
harness happens to offer. A harness with a native human prompt may use it; a
harness without one must refuse.

---

## 3. Status per harness

Legend: ✅ verified · ⚠️ implemented, not yet verified live · ❌ broken

| harness | (a) loads | (b) approvals | (c) questions route | (d) failure posture |
|---|---|---|---|---|
| claude | ✅ hook executes in-sandbox | ✅ sandbox-tested (`allow`/`reject`/`explain`, `auto_allow`) | ✅ sandbox-tested in all three routing modes | ✅ falls open to native prompt, tested |
| opencode v1 | ⚠️ config entry now always written; **not live-verified** | ❌ no shim — v1 never fires `permission.ask` | ⚠️ daemon contract covered; shim load unproven | ⚠️ |
| opencode2 | ⚠️ verified live once (question path) | ⚠️ wire-level only, never live | ✅ verified live | ⚠️ `fallback` never allow — wire-tested |
| pi | ⚠️ extension staged, never live-verified | ⚠️ fail-closed, wire-tested | ⚠️ `capctl ask` only | ✅ fail-closed, wire-tested |
| generic | ❌ no shim | ❌ refuses `capwrap` | ❌ | ❌ |

Legend for the honest cells: ✅ = evidence at tier 2 or higher, ⚠️ = correct by
construction and/or tier 1, but no harness has been shown to load it.

**No cell becomes ✅ on the strength of a unit test.** A shim that is correct
and never loaded is the exact failure this document exists to prevent. In
particular (a) is the weakest cell across the board: only opencode2 has ever
been *observed* loading its plugin, and that observation predates this
milestone's fixes.

---

## 4. The verification tiers

| tier | what it proves | needs a model? | gate |
|---|---|---|---|
| **1 — wire conformance** | each shim speaks the daemon protocol correctly: request shape, blocking, answer propagation, failure posture | no | CI |
| **2 — sandbox E2E** | the daemon routes and resolves inside a real bubblewrap container, driven by a deterministic injection point | no | CI (`@pytest.mark.sandbox`) |
| **3 — pinned-model smoke** | a real harness binary boots, loads its shim, and can raise one question/approval | yes | `CAPWRAP_E2E_SMOKE=1` only |

### Injection points (model cooperation is never an assertion)

| harness | force a question | force an approval | prove the shim loaded |
|---|---|---|---|
| claude | synthetic `AskUserQuestion` PreToolUse JSON piped to `hook.py` | synthetic `PreToolUse` JSON | hook output (deterministic) |
| pi | `capctl ask "Q" --options a,b` as the container `command` | `pi-extension.ts` `tool_call` via tier-1 fake ctx | extension file present + load log |
| opencode v1 | shim `question` hook via tier-1 fake ctx; daemon-side `ask` for tier 2 | native (no shim) | harness load log |
| opencode2 | shim `question` tool via tier-1 fake ctx | `permission.evaluate` via tier-1 fake ctx | `msg="loading plugin"` in scrollback |

Tier 3 exists only to close the gap tier 1/2 cannot: that a *real* harness
loads the shim. Its prompts are bounded and text-only (`reply OK`) plus at most
one forced trigger, and a non-compliant model fails the **smoke** run without
turning CI red.

Test agents are pinned to `opencode-go/glm-5.3-flash` and are given **no real
work** — they exist solely to exercise these paths.

---

## 5. Rules for anyone changing a shim

1. **Send `kind` explicitly.** Never rely on the absence of a `tool` key.
2. **Never invent an empty permission block.** Skipping a config file is safe
   only when the profile has no `plugin_entry`; the `plugins` array is that
   file's *loading mechanism*, so gating the file on content alone silently
   unloads the shim (GAP A).
3. **Fail toward the human.** Never auto-allow on daemon loss.
4. **A hook that returns `output` without an output schema breaks opencode2.**
   Return `{content}` only.
5. **Never claim a harness works on a unit test.** Point at tier 1/2/3.
