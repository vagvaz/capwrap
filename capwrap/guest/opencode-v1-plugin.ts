/**
 * capwrap question shim for opencode v1 — route the native `question` tool
 * to the operator's console.
 *
 * Dropped at `/home/agent/.config/opencode/plugins/capwrap.ts` by capwrap's
 * fsprep (the config dir is copied into the container). opencode v1
 * auto-loads `.ts` plugins from that directory (Bun runtime, no build
 * step), so this file stays dependency-free: `node:` builtins only, no
 * `@opencode-ai/plugin` import.
 *
 * v1's plugin API exposes tool hooks but no permission hooks (unlike the
 * v2 `permission.evaluate` API this build's v2 shim uses), so permissions
 * stay native here — v1 enforces the merged config's own allow/deny rules
 * and prompts in its own TUI. This shim is questions only: the agent's
 * native `question` tool is intercepted before execution and routed
 * through the daemon, where the container's question routing (forward /
 * block / auto) decides who answers, and the console's Questions tab
 * surfaces it with any options as answer chips.
 *
 * The answer text -- the operator's reply, the auto-answer, or the block
 * guidance -- is thrown as the tool's error message, which is what the
 * model reads. Fail-safe: if the daemon cannot be reached, the hook falls
 * through and v1's own question UI asks locally, exactly as without the
 * shim.
 *
 * Protocol (mirrors hook.py and the v2 shim exactly): one JSON line over
 * the daemon's unix socket, newline-delimited, blocking until answered.
 *
 *     → {"id":1,"op":"ask","args":{"question":...,"context":{...},"block":true,"timeout":3600}}
 *     ← {"ok":true,"result":{"decision":...,"message":...}}
 */

import net from "node:net";

const SOCKET = process.env.CAPWRAP_SOCKET ?? "/run/capwrap.sock";
//: Long, because the whole design is that a human answers this.
const ASK_TIMEOUT = Number(process.env.CAPWRAP_ASK_TIMEOUT ?? "3600") || 3600;
const CONTAINER = process.env.CAPWRAP_CONTAINER ?? "?";

function askDaemon(
  question: string,
  context: Record<string, unknown>,
): Promise<{ decision?: string; reason?: string; message?: string }> {
  return new Promise((resolve, reject) => {
    const sock = net.createConnection(SOCKET);
    let buffer = "";
    let settled = false;

    const cleanup = () => {
      sock.off("data", onData);
      sock.off("error", onError);
      sock.off("close", onClose);
      sock.off("timeout", onTimeout);
      sock.destroy();
    };
    const fail = (err: Error) => {
      if (settled) return;
      settled = true;
      cleanup();
      reject(err);
    };
    const onData = (chunk: Buffer) => {
      buffer += chunk.toString("utf8");
      const nl = buffer.indexOf("\n");
      if (nl === -1 || settled) return;
      settled = true;
      cleanup();
      const line = buffer.slice(0, nl);
      try {
        const reply = JSON.parse(line) as {
          ok?: boolean;
          result?: { decision?: string; reason?: string; message?: string };
          error?: { message?: string };
        };
        if (!reply.ok) {
          reject(new Error(reply.error?.message ?? "request failed"));
        } else {
          resolve(reply.result ?? {});
        }
      } catch (err) {
        reject(err instanceof Error ? err : new Error(String(err)));
      }
    };
    const onError = (err: Error) => fail(err);
    const onClose = () => fail(new Error("the daemon closed the connection"));
    const onTimeout = () =>
      fail(
        new Error(`timed out waiting for the capwrap daemon (${ASK_TIMEOUT}s)`),
      );

    sock.on("data", onData);
    sock.on("error", onError);
    sock.on("close", onClose);
    sock.on("timeout", onTimeout);
    sock.setTimeout(Math.max(1, Math.round(ASK_TIMEOUT * 1000)));
    try {
      sock.write(
        JSON.stringify({
          id: 1,
          op: "ask",
          args: { question, context, block: true, timeout: ASK_TIMEOUT },
        }) + "\n",
      );
    } catch (err) {
      fail(err instanceof Error ? err : new Error(String(err)));
    }
  });
}

export const CapwrapQuestions = async (ctx: unknown) => {
  void ctx;
  return {
    "tool.execute.before": async (input: any, output: any) => {
      if (String(input?.tool ?? "") !== "question") return;
      const questions = Array.isArray(output?.args?.questions)
        ? output.args.questions
        : [];
      if (!questions.length) return;
      const text = questions
        .map((q: any) => (typeof q === "string" ? q : String(q?.question ?? "")))
        .filter(Boolean)
        .join("\n");
      if (!text) return;
      const options = questions.flatMap((q: any) => {
        if (typeof q !== "object" || q === null || !Array.isArray(q.options))
          return [];
        return q.options.map((o: any) =>
          typeof o === "string" ? o : String(o?.label ?? o ?? ""),
        );
      });
      let result: { decision?: string; reason?: string; message?: string };
      try {
        result = await askDaemon(text, {
          container: CONTAINER,
          session: String(input?.sessionID ?? ""),
          ...(options.length ? { options } : {}),
        });
      } catch {
        return; // daemon unreachable: fall through to v1's native question UI
      }
      const answer = result.message || result.reason || "";
      // Any routed answer (operator text, auto-answer, block guidance)
      // becomes the tool's error message. A timeout with no text falls
      // through to the native UI.
      if (answer) throw new Error(answer);
    },
  };
};
