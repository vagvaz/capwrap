/**
 * Wire-conformance tests for the opencode v1 question shim.
 *
 * v1's plugin API exposes tool hooks but no permission hooks, so this shim is
 * questions only: it intercepts the native `question` tool before execution,
 * routes it through the daemon, and -- because v1 ignores hook return values
 * -- delivers the operator's answer as a thrown, prefixed Error.
 *
 * What must hold:
 *   - the request shape matches what the daemon speaks (op, block, joined
 *     question text, options flattened into context),
 *   - the answer comes back as a thrown prefixed error telling the model not
 *     to retry,
 *   - a non-question tool is never touched,
 *   - an unreachable daemon falls through to v1's native question UI instead
 *     of throwing (fail-open: conversation continues locally),
 *   - an answer-less reply also falls through rather than throwing an empty
 *     error.
 *
 * Run with: bun test tests/*.test.ts   (or: make test-shims)
 */
import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import {
  answerReply,
  errorReply,
  explainReply,
  rejectReply,
  startFakeDaemon,
  type FakeDaemon,
} from "./shim_harness/fake_daemon.ts";

const SOCKET_DIR = fs.mkdtempSync(path.join(os.tmpdir(), "capwrap-v1-shim-"));
const SOCKET_PATH = path.join(SOCKET_DIR, "capwrap.sock");

// The shim reads its endpoints at import time, so both the socket path and
// the container name have to be set before the module loads.
process.env.CAPWRAP_SOCKET = SOCKET_PATH;
process.env.CAPWRAP_CONTAINER = "v1-conformance";
// Short: the fake daemon answers immediately, and the unreachable test must
// fail fast rather than wait out the production 3600s default.
process.env.CAPWRAP_ASK_TIMEOUT = "5";

const daemon: FakeDaemon = await startFakeDaemon(SOCKET_DIR, "capwrap.sock");
const { CapwrapQuestions } = await import(
  "../capwrap/guest/opencode-v1-plugin.ts"
);

type BeforeFn = (
  input: Record<string, unknown>,
  output: Record<string, unknown>,
) => Promise<void>;

const hooks = await CapwrapQuestions({});
const before = hooks["tool.execute.before"] as BeforeFn;

/** The questions array v1 passes in `output.args`. */
function outputFor(...questions: unknown[]): Record<string, unknown> {
  return { args: { questions } };
}

/** Most recent `ask` request, parsed. */
function lastAsk(): Record<string, any> {
  const asks = daemon.requests.filter((r) => r.parsed.op === "ask");
  expect(asks.length).toBeGreaterThan(0);
  return asks[asks.length - 1].parsed;
}

function clear(): void {
  daemon.requests.length = 0;
}

beforeAll(() => {
  daemon.on("ask", answerReply("placeholder"));
});

describe("opencode v1 question shim — routing to the daemon", () => {
  test("delivers the operator's answer as a thrown, prefixed error", async () => {
    clear();
    daemon.on("ask", answerReply("use postgres"));
    const input = { tool: "question", sessionID: "sess-1" };

    let thrown: Error | null = null;
    try {
      await before(input, outputFor({ question: "which database?" }));
    } catch (err) {
      thrown = err as Error;
    }

    expect(thrown).toBeInstanceOf(Error);
    const message = thrown!.message;
    // v1 ignores hook returns: this Error IS the tool result, and it must
    // tell the model not to retry the question tool with it.
    expect(message).toContain(
      "capwrap: this is the answer to your question",
    );
    expect(message).toContain("do not call the question tool again");
    expect(message).toContain("use postgres");
  });

  test("sends the documented wire shape", async () => {
    clear();
    daemon.on("ask", answerReply("ok"));
    await before(
      { tool: "question", sessionID: "sess-2" },
      outputFor({ question: "ship it?" }),
    ).catch(() => undefined);

    const req = lastAsk();
    expect(req.id).toBe(1);
    expect(req.op).toBe("ask");
    expect(req.args.block).toBe(true);
    expect(req.args.question).toBe("ship it?");
    expect(req.args.timeout).toBeNumber();
    const context = req.args.context as Record<string, unknown>;
    expect(context.container).toBe("v1-conformance");
    expect(context.session).toBe("sess-2");
    // A question is not a permission: no tool key, and v1 does not stamp a
    // kind -- absence of `tool` is what makes the daemon classify it as a
    // question.
    expect(context.tool).toBeUndefined();
    expect(context.kind).toBeUndefined();
  });

  test("joins multiple questions and flattens options into context", async () => {
    clear();
    daemon.on("ask", answerReply("sqlite"));
    await before(
      { tool: "question", sessionID: "sess-3" },
      outputFor(
        { question: "first?", options: [{ label: "yes" }, { label: "no" }] },
        { question: "second?", options: ["alpha", "beta"] },
      ),
    ).catch(() => undefined);

    const req = lastAsk();
    expect(req.args.question).toBe("first?\nsecond?");
    expect((req.args.context as any).options).toEqual([
      "yes",
      "no",
      "alpha",
      "beta",
    ]);
  });

  test("omits options entirely when the questions carry none", async () => {
    clear();
    daemon.on("ask", answerReply("fine"));
    await before(
      { tool: "question", sessionID: "sess-4" },
      outputFor({ question: "any preference?" }),
    ).catch(() => undefined);

    expect((lastAsk().args.context as any).options).toBeUndefined();
  });

  test("uses the explain reply's message as the answer", async () => {
    clear();
    daemon.on("ask", explainReply("narrow it to one migration"));
    let thrown: Error | null = null;
    try {
      await before(
        { tool: "question" },
        outputFor({ question: "how should I migrate?" }),
      );
    } catch (err) {
      thrown = err as Error;
    }
    expect(thrown!.message).toContain("narrow it to one migration");
  });

  test("uses the reject reply's reason as the answer", async () => {
    clear();
    daemon.on("ask", rejectReply("not that one"));
    let thrown: Error | null = null;
    try {
      await before({ tool: "question" }, outputFor({ question: "delete it?" }));
    } catch (err) {
      thrown = err as Error;
    }
    expect(thrown!.message).toContain("not that one");
  });
});

describe("opencode v1 question shim — what it must NOT do", () => {
  test("leaves every other tool alone", async () => {
    clear();
    daemon.on("ask", answerReply("should not arrive"));
    const result = await before(
      { tool: "Bash", sessionID: "sess-5" },
      outputFor({ question: "ignored" }),
    );
    expect(result).toBeUndefined();
    expect(daemon.countOf("ask")).toBe(0);
  });

  test("leaves a question with no usable text alone", async () => {
    clear();
    const result = await before({ tool: "question" }, { args: { questions: [] } });
    expect(result).toBeUndefined();
    expect(daemon.countOf("ask")).toBe(0);
  });

  test("falls through when the reply carries no answer text", async () => {
    clear();
    // decision with neither message nor reason: nothing to tell the model,
    // so the native UI should ask rather than throw an empty error.
    daemon.on("ask", { kind: "ok", result: { decision: "allow" } });
    let threw = false;
    try {
      await before({ tool: "question" }, outputFor({ question: "quiet?" }));
    } catch {
      threw = true;
    }
    expect(threw).toBe(false);
  });

  test("falls through when the daemon answers with a wire error", async () => {
    clear();
    daemon.on("ask", errorReply("capability_denied", "operator refused"));
    let threw = false;
    try {
      await before({ tool: "question" }, outputFor({ question: "refused?" }));
    } catch {
      threw = true;
    }
    expect(threw).toBe(false);
  });
});

describe("opencode v1 question shim — daemon unreachable", () => {
  test("falls through to v1's native question UI instead of throwing", async () => {
    // Close the server: the path still exists but nothing listens, so the
    // connect fails the same way a dead daemon does.
    await daemon.close();
    clear();

    let thrown: unknown = null;
    let result: unknown = null;
    try {
      result = await before(
        { tool: "question" },
        outputFor({ question: "still there?" }),
      );
    } catch (err) {
      thrown = err;
    }

    // Fail-open by design: no daemon means v1 asks locally, exactly as it
    // would without the shim. Silence is the correct answer here.
    expect(thrown).toBeNull();
    expect(result).toBeUndefined();
    expect(daemon.countOf("ask")).toBe(0);
  });
});

afterAll(() => {
  fs.rmSync(SOCKET_DIR, { recursive: true, force: true });
});
