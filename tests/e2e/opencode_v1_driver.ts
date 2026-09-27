/**
 * e2e driver: call opencode v1's shipped question shim the way opencode v1
 * would, inside the real sandbox, against the real daemon.
 *
 * The guest dir binds read-only at /opt/capwrap in every container, so the
 * driver imports the shipped shim in place (no copy, no build step). It
 * invokes `hooks["tool.execute.before"]` exactly as opencode v1 does when the
 * model calls its native `question` tool: input carries the tool name and
 * session, output carries the tool's arguments.
 *
 * Two outcomes are possible and both are reported on one machine-readable
 * line (`CAPWRAP_E2E_RESULT:<json>`, asserted by tests/test_harness_e2e.py):
 *
 *   - the hook THREW -- the routed answer (operator text, auto-answer or
 *     block guidance) was delivered as the prefixed Error that v1 reads,
 *   - the hook RETURNED undefined -- the fall-through posture: the daemon
 *     was unreachable, so v1's own question UI asks locally.
 */
function shimEnv(): Record<string, string | null> {
  return {
    socket: process.env.CAPWRAP_SOCKET ?? null,
    container: process.env.CAPWRAP_CONTAINER ?? null,
  };
}

const { CapwrapQuestions } = await import("/opt/capwrap/opencode-v1-plugin.ts");
const hooks = await CapwrapQuestions({});
try {
  await hooks["tool.execute.before"](
    { tool: "question", sessionID: "e2e-s" },
    { args: { questions: [{ question: "E2E question?" }] } },
  );
  console.log(
    "CAPWRAP_E2E_RESULT:" +
      JSON.stringify({ outcome: "fell-through", env: shimEnv() }),
  );
} catch (err: any) {
  console.log(
    "CAPWRAP_E2E_RESULT:" +
      JSON.stringify({
        outcome: "threw",
        message: String(err?.message ?? err),
        env: shimEnv(),
      }),
  );
}
