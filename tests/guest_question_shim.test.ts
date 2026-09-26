/**
 * The v2 question shim: `ctx.tool.transform` adds a `question` tool whose
 * execute asks the daemon and returns a normal result. With no daemon, the
 * result must still be normal -- a thrown Error reads as a tool failure and
 * the model would retry.
 *
 * Run with: bun test tests/guest_question_shim.test.ts
 */
import { afterAll, describe, expect, test } from "bun:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

// The shim reads its endpoints at import time, so the missing socket has to
// exist before the module loads.
const SOCKET_DIR = fs.mkdtempSync(path.join(os.tmpdir(), "capwrap-shim-"));
const MISSING_SOCKET = path.join(SOCKET_DIR, "no-such-capwrap.sock");
const POLICY_PATH = path.join(SOCKET_DIR, "policy.json");
process.env.CAPWRAP_SOCKET = MISSING_SOCKET;
process.env.CAPWRAP_POLICY = POLICY_PATH;
// Kept short: the socket is missing, so the connection fails immediately;
// the timeout only has to be shorter than the test's failure timeout.
process.env.CAPWRAP_ASK_TIMEOUT = "5";

const plugin = (await import("../capwrap/guest/opencode-plugin.ts")).default;

describe("opencode2 question shim", () => {
  const added: any[] = [];
  let unusedHook: string | null = null;
  const ctx = {
    tool: {
      transform: async (fn: any) => {
        fn({
          add: (tool: any) => {
            added.push(tool);
          },
        });
      },
      hook: async (name: string, _fn: any) => {
        unusedHook = name;
      },
    },
    permission: {
      hook: async (_name: string, _fn: any) => {},
    },
  };

  test("setup registers a question tool override via transform", async () => {
    await plugin.setup(ctx);
    expect(added).toHaveLength(1);
    const tool = added[0];
    expect(tool.name).toBe("question");
    expect(tool.description).toContain("operator");
    expect(tool.execute).toBeFunction();
  });

  test("execute returns a normal result when the daemon is unreachable", async () => {
    await plugin.setup(ctx);
    const tool = added[0];
    let thrown: unknown = null;
    let result: unknown = null;
    try {
      result = await tool.execute({
        questions: [{ question: "Routing test?", options: [{ label: "yes" }] }],
      });
    } catch (err) {
      thrown = err;
    }
    expect(thrown).toBeNull();
    expect(result).toBeObject();
    const text = String((result as any)?.content ?? "");
    expect(text).toContain("console unreachable");
    expect(text).toContain("Do not call the question tool again");
    expect(String((result as any)?.output ?? "")).toContain(
      "Do not call the question tool again",
    );
    // The transform path wins: no throwing execute.before hook alongside it.
    expect(unusedHook).toBeNull();
  });
});

afterAll(() => {
  fs.rmSync(SOCKET_DIR, { recursive: true, force: true });
});
