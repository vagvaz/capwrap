/**
 * e2e driver: load opencode v2's shipped gate the way opencode v2's plugin
 * API would, inside the real sandbox, against the real daemon.
 *
 * The guest dir binds read-only at /opt/capwrap in every container, so the
 * driver imports the shipped shim in place (a plugin DIRECTORY, `index.ts`,
 * the way v2 loads it). `setup()` is called with a fake plugin context whose
 * `tool.transform` captures the editor callback, which is what registers the
 * `question` tool override -- including its `execute`. The driver then calls
 * that `execute` with the same arguments the model would supply.
 *
 * The outcome is reported on one machine-readable line
 * (`CAPWRAP_E2E_RESULT:<json>`, asserted by tests/test_harness_e2e.py):
 * the routed answer comes back as a normal tool result whose content begins
 * "User has answered your questions", or -- when the daemon is unreachable --
 * as content that hands the question back to the agent's own terminal.
 */
function shimEnv(): Record<string, string | null> {
  return {
    socket: process.env.CAPWRAP_SOCKET ?? null,
    container: process.env.CAPWRAP_CONTAINER ?? null,
  };
}

const plugin = (await import("/opt/capwrap/opencode2-plugin/index.ts"))
  .default;
const env = shimEnv();

let questionTool: any = null;
let evaluateRegistered = false;
await plugin.setup({
  tool: {
    transform: async (fn: any) => {
      await fn({
        add: (tool: any) => {
          questionTool = tool;
        },
      });
    },
  },
  permission: {
    hook: async (_name: string, _fn: any) => {
      evaluateRegistered = true;
    },
  },
});

if (!questionTool || questionTool.name !== "question") {
  console.log(
    "CAPWRAP_E2E_RESULT:" +
      JSON.stringify({
        outcome: "no-question-tool",
        got: questionTool && questionTool.name,
        env,
      }),
  );
} else {
  const result = await questionTool.execute({
    sessionID: "e2e-s",
    questions: [
      {
        question: "E2E question?",
        options: [{ label: "main" }, { label: "release" }],
      },
    ],
  });
  console.log(
    "CAPWRAP_E2E_RESULT:" +
      JSON.stringify({
        outcome: "result",
        content: result?.content ?? "",
        env,
      }),
  );
}
