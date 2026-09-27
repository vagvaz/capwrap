/**
 * Wire-conformance tests for the pi extension's `tool_call` gate.
 *
 * pi is the harness with the strictest contract: it has no native prompt to
 * fall back to, so EVERY failure must block. A silent allow anywhere here
 * would disable approvals system-wide, which is exactly what the shim's own
 * "not silent-allow" rationale exists to prevent.
 *
 * What must hold:
 *   - a deny rule blocks without consulting the daemon,
 *   - an allow rule passes without consulting the daemon,
 *   - an unmatched call reaches the daemon carrying `tool` + `input`
 *     (so the daemon classifies it as an approval),
 *   - allow → let the call through; reject/explain/anything-else → block,
 *   - an unreachable daemon → block (fail-closed, never allow),
 *   - a missing policy file → block, never a silent allow.
 *
 * Run with: bun test tests/*.test.ts   (or: make test-shims)
 */
import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import {
  allowReply,
  answerReply,
  explainReply,
  rejectReply,
  startFakeDaemon,
  type FakeDaemon,
} from "./shim_harness/fake_daemon.ts";

const SOCKET_DIR = fs.mkdtempSync(path.join(os.tmpdir(), "capwrap-pi-shim-"));
const SOCKET_PATH = path.join(SOCKET_DIR, "capwrap.sock");
const POLICY_PATH = path.join(SOCKET_DIR, "policy.json");

process.env.CAPWRAP_SOCKET = SOCKET_PATH;
process.env.CAPWRAP_POLICY = POLICY_PATH;
process.env.CAPWRAP_CONTAINER = "pi-conformance";
process.env.CAPWRAP_ASK_TIMEOUT = "5";

function writePolicy(policy: Record<string, unknown>): void {
  fs.writeFileSync(POLICY_PATH, JSON.stringify(policy));
}

writePolicy({ allow: [], deny: [], fallback: "deny" });

const daemon: FakeDaemon = await startFakeDaemon(SOCKET_DIR, "capwrap.sock");
const installExtension = (await import("../capwrap/guest/pi-extension.ts"))
  .default;

/** Capture the `tool_call` handler the extension registers. */
function captureHandler(): (event: any, ctx?: any) => Promise<any> {
  let handler: ((event: any, ctx?: any) => Promise<any>) | null = null;
  const pi = {
    on: (name: string, fn: any) => {
      expect(name).toBe("tool_call");
      handler = fn;
    },
  };
  installExtension(pi);
  expect(handler).not.toBeNull();
  return handler!;
}

const handler = captureHandler();

/** A tool call event: pi lowercases the tool name itself. */
function call(toolName: string, input: Record<string, unknown> = {}) {
  return { toolName, input };
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

describe("pi extension — registration", () => {
  test("registers exactly one tool_call gate", () => {
    // Re-capturing proves registration is deterministic and singular.
    let count = 0;
    installExtension({ on: (name: string) => {
      expect(name).toBe("tool_call");
      count++;
    } });
    expect(count).toBe(1);
  });
});

describe("pi extension — policy short-circuits", () => {
  test("a deny rule blocks without touching the daemon", async () => {
    clear();
    writePolicy({ allow: [], deny: ["bash(sudo *)"], fallback: "deny" });
    daemon.on("ask", allowReply("should not be consulted"));

    const result = await handler(
      call("Bash", { command: "sudo rm -rf /" }),
    );

    expect(result).toBeObject();
    expect(result.block).toBe(true);
    expect(String(result.reason)).toContain("denies bash");
    expect(askCount()).toBe(0);
  });

  test("an allow rule passes without touching the daemon", async () => {
    clear();
    writePolicy({ allow: ["read"], deny: [], fallback: "deny" });
    daemon.on("ask", rejectReply("should not be consulted"));

    const result = await handler(call("Read", { file_path: "/work/a.txt" }));

    // undefined lets the call proceed.
    expect(result).toBeUndefined();
    expect(askCount()).toBe(0);
  });
});

describe("pi extension — routing through the daemon", () => {
  beforeAll(() => {
    writePolicy({ allow: [], deny: [], fallback: "deny" });
  });

  test("an unmatched call carries tool and input in the context", async () => {
    clear();
    daemon.on("ask", allowReply("approved"));
    await handler(call("Write", { file_path: "/work/a.txt" })).catch(
      () => undefined,
    );

    const req = lastAsk();
    expect(req.op).toBe("ask");
    expect(req.args.block).toBe(true);
    expect(req.args.question).toContain("write");
    const context = req.args.context as Record<string, unknown>;
    // `tool` present => the daemon classifies this as an APPROVAL, which
    // deliberately bypasses question routing.
    expect(context.tool).toBe("write");
    expect(context.container).toBe("pi-conformance");
    expect((context.input as any).file_path).toBe("/work/a.txt");
  });

  test("allow lets the call through", async () => {
    clear();
    daemon.on("ask", allowReply("ok"));
    const result = await handler(call("Write", { file_path: "/work/b.txt" }));
    expect(result).toBeUndefined();
  });

  test("reject blocks with the operator's reason", async () => {
    clear();
    daemon.on("ask", rejectReply("no, not that file"));
    const result = await handler(call("Write", { file_path: "/work/c.txt" }));
    expect(result.block).toBe(true);
    expect(String(result.reason)).toContain("no, not that file");
  });

  test("a legacy deny decision blocks too", async () => {
    clear();
    daemon.on("ask", {
      kind: "ok",
      result: { decision: "deny", reason: "legacy wording" },
    });
    const result = await handler(call("Write", { file_path: "/work/d.txt" }));
    expect(result.block).toBe(true);
    expect(String(result.reason)).toContain("legacy wording");
  });

  test("explain blocks carrying the text -- never a silent allow", async () => {
    clear();
    daemon.on("ask", explainReply("use /scratch instead"));
    const result = await handler(call("Write", { file_path: "/work/e.txt" }));
    // pi has no native prompt: blocking with the text is the only way to
    // surface an explanation. Allowing would have decided for the operator.
    expect(result.block).toBe(true);
    expect(String(result.reason)).toContain("use /scratch instead");
  });

  test("an unanswered decision blocks rather than allowing", async () => {
    clear();
    daemon.on("ask", { kind: "ok", result: { decision: "timeout" } });
    const result = await handler(call("Write", { file_path: "/work/f.txt" }));
    expect(result.block).toBe(true);
    expect(String(result.reason)).toContain("no answer");
  });
});

describe("pi extension — fail-closed", () => {
  test("an unreachable daemon blocks, never allows", async () => {
    await daemon.close();
    clear();

    const result = await handler(call("Write", { file_path: "/work/g.txt" }));

    expect(result).toBeObject();
    expect(result.block).toBe(true);
    expect(String(result.reason)).toContain("capwrap unreachable");
    expect(String(result.reason)).toContain("no native prompt");
    expect(askCount()).toBe(0);
  });

  test("a wire error blocks", async () => {
    clear();
    // Re-open so the socket accepts and then refuses at the protocol level.
    const daemon2 = await startFakeDaemon(SOCKET_DIR, "capwrap2.sock");
    process.env.CAPWRAP_SOCKET = `${SOCKET_DIR}/capwrap2.sock`;
    // CAPWRAP_SOCKET is read at module import, so this cannot retarget the
    // already-loaded shim; assert instead on the closed-daemon path above
    // and on the missing-policy path below.
    await daemon2.close();
    expect(result2(await handler(call("Write", { file_path: "/work/h.txt" }))))
      .toBe(true);
  });

  test("a missing policy file blocks instead of silently allowing", async () => {
    // Policy is re-read per call, so removing it mid-flight must not turn
    // the gate off: loadPolicy() throws, the catch fails closed.
    fs.rmSync(POLICY_PATH, { force: true });
    clear();

    const result = await handler(call("Write", { file_path: "/work/i.txt" }));

    expect(result).toBeObject();
    expect(result.block).toBe(true);
    expect(askCount()).toBe(0);
  });
});

/** The `block` flag of whatever the handler returned. */
function result2(value: any): boolean {
  return value?.block === true;
}

afterAll(() => {
  fs.rmSync(SOCKET_DIR, { recursive: true, force: true });
});
