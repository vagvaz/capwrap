/**
 * Wire-conformance tests for the opencode2 shim: the `question` tool
 * override and the `permission.evaluate` hook.
 *
 * The question path must return `{content}` ONLY -- a result carrying
 * `output` without an output schema makes v2 error, and the model reads that
 * as a failure and retries the question tool. The permission path must carry
 * `tool` in its context (so the daemon classifies it as an approval, which
 * bypasses question routing), honour allow/reject/explain, and on daemon loss
 * obey the policy file's `fallback` -- never allowing.
 *
 * Run with: bun test tests/*.test.ts   (or: make test-shims)
 */
import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import {
  answerReply,
  allowReply,
  explainReply,
  rejectReply,
  startFakeDaemon,
  type FakeDaemon,
} from "./shim_harness/fake_daemon.ts";

const SOCKET_DIR = fs.mkdtempSync(path.join(os.tmpdir(), "capwrap-oc2-shim-"));
const SOCKET_PATH = path.join(SOCKET_DIR, "capwrap.sock");
const POLICY_PATH = path.join(SOCKET_DIR, "policy.json");

process.env.CAPWRAP_SOCKET = SOCKET_PATH;
process.env.CAPWRAP_POLICY = POLICY_PATH;
process.env.CAPWRAP_CONTAINER = "oc2-conformance";
process.env.CAPWRAP_ASK_TIMEOUT = "5";

/** No rules, no ask floor: the permission path must reach the daemon. */
function writePolicy(policy: Record<string, unknown>): void {
  fs.writeFileSync(POLICY_PATH, JSON.stringify(policy));
}

writePolicy({ allow: [], deny: [], fallback: "deny" });

const daemon: FakeDaemon = await startFakeDaemon(SOCKET_DIR, "capwrap.sock");
const plugin = (await import("../capwrap/guest/opencode2-plugin/index.ts"))
  .default;

type Ctx = {
  ctx: any;
  tools: any[];
  permissionHandler: (() => Promise<void>) | null;
  permissionHookName: string | null;
};

function makeCtx(): Ctx {
  const built: Ctx = {
    ctx: null,
    tools: [],
    permissionHandler: null,
    permissionHookName: null,
  };
  built.ctx = {
    tool: {
      transform: async (fn: any) => {
        fn({ add: (tool: any) => built.tools.push(tool) });
      },
      hook: async () => {
        /* the transform path must win; the fallback must not be needed */
      },
    },
    permission: {
      hook: async (name: string, fn: any) => {
        built.permissionHookName = name;
        built.permissionHandler = fn;
      },
    },
  };
  return built;
}

/** The permission hook, registered by `setup`. */
async function evaluate(handler: any, event: Record<string, unknown>) {
  await handler(event);
  return event;
}

function lastAsk(): Record<string, any> {
  const asks = daemon.requests.filter((r) => r.parsed.op === "ask");
  expect(asks.length).toBeGreaterThan(0);
  return asks[asks.length - 1].parsed;
}

function askCount(): number {
  return daemon.countOf("ask");
}

function clear(): void {
  daemon.requests.length = 0;
}

let registered: Ctx;

beforeAll(async () => {
  registered = makeCtx();
  await plugin.setup(registered.ctx);
});

describe("opencode2 shim — setup", () => {
  test("registers exactly one question tool override", () => {
    expect(registered.tools).toHaveLength(1);
    const tool = registered.tools[0];
    expect(tool.name).toBe("question");
    expect(tool.description).toContain("operator");
    // It must also say it is not the permission channel.
    expect(tool.description).toContain("permission");
    expect(tool.execute).toBeFunction();
    expect(tool.input).toBeObject();
    expect(tool.input.required).toEqual(["questions"]);
  });

  test("registers the permission.evaluate hook with a policy present", () => {
    expect(registered.permissionHookName).toBe("evaluate");
    expect(registered.permissionHandler).toBeFunction();
  });
});

describe("opencode2 shim — question tool", () => {
  test("returns the answer as content, with no output key", async () => {
    clear();
    daemon.on("ask", answerReply("postgres"));
    const tool = registered.tools[0];

    let thrown: unknown = null;
    let result: any = null;
    try {
      result = await tool.execute({
        questions: [{ question: "which database?" }],
      });
    } catch (err) {
      thrown = err;
    }

    // A thrown Error reads as a tool failure and the model retries. It must
    // be a normal result.
    expect(thrown).toBeNull();
    expect(result).toBeObject();
    expect(result.content).toContain("User has answered your questions");
    expect(result.content).toContain('"which database?"="postgres"');
    expect(result.content).toContain("You can now continue");
    // v2 rejects `output` when the tool declares no output schema.
    expect(result.output).toBeUndefined();
  });

  test("sends the documented wire shape with no tool in the question context", async () => {
    clear();
    daemon.on("ask", answerReply("sqlite"));
    await registered.tools[0].execute({
      questions: [{ question: "ship it?", options: [{ label: "yes" }] }],
    }).catch(() => undefined);

    const req = lastAsk();
    expect(req.op).toBe("ask");
    expect(req.args.block).toBe(true);
    expect(req.args.question).toBe("ship it?");
    const context = req.args.context as Record<string, unknown>;
    expect(context.container).toBe("oc2-conformance");
    expect(context.options).toEqual(["yes"]);
    // Questions are conversation: no tool, no kind -- the absence of `tool`
    // is what makes the daemon route it as a question.
    expect(context.tool).toBeUndefined();
    expect(context.kind).toBeUndefined();
  });

  test("joins several questions and answers them in one result", async () => {
    clear();
    daemon.on("ask", answerReply("sqlite"));
    const result = await registered.tools[0].execute({
      questions: [{ question: "first?" }, { question: "second?" }],
    });
    expect(lastAsk().args.question).toBe("first?\nsecond?");
    expect(result.content).toContain('"first?"="sqlite"');
    expect(result.content).toContain('"second?"="sqlite"');
  });

  test("rejects a call with no questions instead of asking", async () => {
    clear();
    await expect(
      registered.tools[0].execute({ questions: [] }),
    ).rejects.toThrow(/no questions/i);
    expect(askCount()).toBe(0);
  });
});

describe("opencode2 shim — permission hook", () => {
  test("a deny rule short-circuits without touching the daemon", async () => {
    clear();
    writePolicy({ allow: [], deny: ["bash(sudo *)"], fallback: "deny" });
    daemon.on("ask", allowReply("should not be consulted"));
    const event = await evaluate(registered.permissionHandler, {
      action: "bash",
      resources: ["sudo", "rm"],
      sessionID: "s1",
    });
    expect(event.effect).toBe("deny");
    expect(String(event.message)).toContain("denies");
    expect(askCount()).toBe(0);
  });

  test("an allow rule short-circuits without touching the daemon", async () => {
    clear();
    writePolicy({ allow: ["read"], deny: [], fallback: "deny" });
    daemon.on("ask", rejectReply("should not be consulted"));
    const event = await evaluate(registered.permissionHandler, {
      action: "read",
      resources: [],
      sessionID: "s2",
    });
    expect(event.effect).toBe("allow");
    expect(askCount()).toBe(0);
  });

  test("an unmatched call reaches the daemon with `tool` in the context", async () => {
    clear();
    writePolicy({ allow: [], deny: [], fallback: "deny" });
    daemon.on("ask", allowReply("approved"));
    const event = await evaluate(registered.permissionHandler, {
      action: "bash",
      resources: ["git", "push"],
      sessionID: "s3",
    });

    const req = lastAsk();
    expect(req.op).toBe("ask");
    const context = req.args.context as Record<string, unknown>;
    // Carrying `tool` is what makes the daemon classify this as an
    // APPROVAL, which deliberately bypasses question routing.
    expect(context.tool).toBe("bash");
    expect(req.args.question).toContain("bash");
    expect(event.effect).toBe("allow");
    expect(String(event.message)).toContain("approved");
  });

  test("a reject decision denies", async () => {
    clear();
    writePolicy({ allow: [], deny: [], fallback: "deny" });
    daemon.on("ask", rejectReply("not allowed here"));
    const event = await evaluate(registered.permissionHandler, {
      action: "bash",
      resources: ["git", "push"],
      sessionID: "s4",
    });
    expect(event.effect).toBe("deny");
    expect(String(event.message)).toContain("not allowed here");
  });

  test("an explain decision leaves the effect untouched but carries the text", async () => {
    clear();
    writePolicy({ allow: [], deny: [], fallback: "deny" });
    daemon.on("ask", explainReply("use a feature branch"));
    const event = await evaluate(registered.permissionHandler, {
      action: "bash",
      resources: ["git", "push"],
      sessionID: "s5",
    });
    // Not decided: opencode falls back to its own computed decision.
    expect(event.effect).toBeUndefined();
    expect(String(event.message)).toContain("use a feature branch");
  });
});

describe("opencode2 shim — daemon unreachable", () => {
  test("a question still returns a normal result telling the model to stop", async () => {
    await daemon.close();
    clear();

    let thrown: unknown = null;
    let result: any = null;
    try {
      result = await registered.tools[0].execute({
        questions: [{ question: "anyone there?" }],
      });
    } catch (err) {
      thrown = err;
    }
    expect(thrown).toBeNull();
    expect(result.content).toContain("capwrap console unreachable");
    expect(result.content).toContain("Do not call the question tool again");
    expect(result.output).toBeUndefined();
  });

  test("an approval with no ask floor DENIES -- never allows", async () => {
    clear();
    writePolicy({ allow: [], deny: [], fallback: "deny" });
    const event = await evaluate(registered.permissionHandler, {
      action: "bash",
      resources: ["git", "push"],
      sessionID: "s6",
    });
    expect(event.effect).toBe("deny");
    expect(String(event.message)).toContain("unreachable");
    expect(String(event.message)).toContain("no ask floor");
  });

  test("an approval with an ask floor leaves the effect to opencode's prompt", async () => {
    clear();
    writePolicy({ allow: [], deny: [], fallback: "ask" });
    const event = await evaluate(registered.permissionHandler, {
      action: "bash",
      resources: ["git", "push"],
      sessionID: "s7",
    });
    // `ask` is the harness's native prompt -- fail toward the human, not
    // toward auto-allow.
    expect(event.effect).toBeUndefined();
    expect(String(event.message)).toContain("opencode's own prompt");
  });
});

describe("opencode2 shim — no policy file", () => {
  test("does not register the permission hook at all", async () => {
    // An unreadable policy must not become deny-all, and must not silently
    // gate the harness: the hook simply is not installed.
    fs.rmSync(POLICY_PATH, { force: true });
    const fresh = makeCtx();
    await plugin.setup(fresh.ctx);

    expect(fresh.permissionHandler).toBeNull();
    expect(fresh.permissionHookName).toBeNull();
    // The question override is independent of the policy and must still load.
    expect(fresh.tools).toHaveLength(1);
    expect(fresh.tools[0].name).toBe("question");
  });
});

afterAll(() => {
  fs.rmSync(SOCKET_DIR, { recursive: true, force: true });
});
