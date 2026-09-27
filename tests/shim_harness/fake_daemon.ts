/**
 * A fake capwrap daemon for the shim wire-conformance tests.
 *
 * Listens on a tmpdir unix socket and speaks the daemon's newline-JSON
 * protocol (see `capwrap/ipc/protocol.py`): one JSON object per line, a
 * request is `{id, op, args}` and a reply is `{id, ok, result}` on success or
 * `{id, ok:false, error:{code, message}}` otherwise (the reply echoes the
 * request's `id`).
 *
 * What the real daemon's `ask` handling (`daemon.py` `ask_operator`) replies
 * depends on the request context: a context naming a `tool` is a *permission
 * request* and its reply carries `{decision, reason}`; a *plain question*
 * (context without `tool`, or `kind: "question"`) is routed by the
 * container's question routing and its reply carries `{decision, message}`
 * (the answer text) or `{decision, reason}`. The shims read
 * `result.message || result.reason` for the answer, so tests program both
 * shapes.
 *
 * Every request line is recorded so tests can assert the exact wire shape
 * (the joined question text, `context`, `tool`, `kind`, `options`, ...).
 * Replies are programmed per op name; anything unprogrammed gets a
 * `protocol_error`, so a shim revision that drifted off the wire fails
 * loudly instead of hanging.
 */

import net from "node:net";

/** Wire shapes the shims must speak (protocol.py Request/Response). */
export interface WireRequest {
  id?: number;
  op?: string;
  args?: Record<string, unknown>;
}

export interface RecordedRequest {
  raw: string;
  parsed: WireRequest;
}

export type ReplyResult = Record<string, unknown>;

export type Reply =
  | { kind: "ok"; result: ReplyResult }
  | { kind: "error"; error: { code: string; message: string } }
  /** Never answer: the shim hangs until its own timeout or a release. */
  | { kind: "delay" }
  /** Accept the connection, then close it without replying. */
  | { kind: "close" };

/** A subprocess-level allow decision (permission requests). */
export function allowReply(reason = "approved in the capwrap console"): Reply {
  return { kind: "ok", result: { decision: "allow", reason } };
}

/** A subprocess-level reject ("reject" is the console's word; "deny" is the
 * legacy alias, and both shims treat them the same way). */
export function rejectReply(reason = "denied in the capwrap console"): Reply {
  return { kind: "ok", result: { decision: "reject", reason } };
}

/** The operator replied with text instead of deciding. */
export function explainReply(message = "try a narrower path first"): Reply {
  return { kind: "ok", result: { decision: "explain", message } };
}

/** Question-routing guidance (routing=block's shape from `_route_question`). */
export function blockReply(
  message = "State your questions as plain text in your terminal.",
): Reply {
  return { kind: "ok", result: { decision: "block", message } };
}

/** An answer to a plain question: the text the model should read. */
export function answerReply(text: string, decision = "allow"): Reply {
  return { kind: "ok", result: { decision, message: text } };
}

/** A wire error (protocol.py encodes CapabilityError messages verbatim). */
export function errorReply(
  code = "capability_denied",
  message = "the operator refused",
): Reply {
  return { kind: "error", error: { code, message } };
}

/**
 * Encode one reply as a single newline-terminated JSON line and send it.
 *
 * The shims parse exactly one line and read `ok` / `result` / `error`, and
 * they ignore the reply id (they settle on the first line they see), so the
 * id is echoed for conformance with `protocol.py` rather than because
 * anything here depends on it.
 *
 * `delay` and `close` never reach this function: `handleLine` handles both
 * before writing.
 */
function writeReply(sock: net.Socket, reply: Reply, id: number): void {
  if (reply.kind === "delay" || reply.kind === "close") return;
  const line =
    reply.kind === "ok"
      ? { id, ok: true, result: reply.result }
      : { id, ok: false, error: reply.error };
  if (sock.destroyed || !sock.writable) return;
  sock.write(`${JSON.stringify(line)}\n`);
}

/** The fake daemon. */
export class FakeDaemon {
  readonly requests: RecordedRequest[] = [];
  private replies = new Map<string, Reply | Reply[]>();
  private sockets = new Set<net.Socket>();
  /** Connections waiting in `delay` mode, released by a `release*` call. */
  private pending: net.Socket[] = [];
  private server: net.Server | null = null;

  constructor(readonly socketPath: string) {}

  /** Program the reply for requests with this op. An array is consumed in
   * order, after which its last element repeats; a single reply repeats. */
  on(op: string, reply: Reply | Reply[]): void {
    this.replies.set(op, reply);
  }

  async listen(): Promise<void> {
    this.server = net.createServer((sock) => {
      this.sockets.add(sock);
      sock.on("close", () => this.sockets.delete(sock));
      sock.on("error", () => {
        /* a shim closing mid-flight is not interesting here */
      });
      let buffer = "";
      sock.on("data", (chunk: Buffer) => {
        buffer += chunk.toString("utf8");
        let nl: number;
        while ((nl = buffer.indexOf("\n")) !== -1) {
          const line = buffer.slice(0, nl);
          buffer = buffer.slice(nl + 1);
          if (line.trim()) this.handleLine(line, sock);
        }
      });
    });

    await new Promise<void>((resolve, reject) => {
      this.server!.once("error", reject);
      this.server!.listen(this.socketPath, () => resolve());
    });
  }

  private handleLine(line: string, sock: net.Socket): void {
    let parsed: WireRequest;
    try {
      parsed = JSON.parse(line) as WireRequest;
    } catch {
      writeReply(
        sock,
        { kind: "error", error: { code: "protocol_error", message: "malformed JSON" } },
        0,
      );
      return;
    }
    this.requests.push({ raw: line, parsed });
    const id = typeof parsed.id === "number" ? parsed.id : 0;
    const op = String(parsed.op ?? "?");
    const spec = this.replies.get(op);
    const reply = Array.isArray(spec)
      ? spec.length > 1
        ? spec.shift()!
        : spec[0]
      : spec;
    if (reply?.kind === "delay") {
      this.pending.push(sock);
      return;
    }
    if (reply?.kind === "close") {
      sock.end();
      return;
    }
    writeReply(
      sock,
      reply ?? {
        kind: "error",
        error: { code: "protocol_error", message: `no reply for ${op}` },
      },
      id,
    );
  }

  /** Release every delayed request with an ok result (arbitrary id: the
   * shims ignore the reply id). */
  releaseWith(result: ReplyResult): void {
    for (const sock of this.pending.splice(0)) {
      writeReply(sock, { kind: "ok", result }, 1);
    }
  }

  /** Release every delayed request with an error. */
  releaseError(message = "slow down", code = "rate_limited"): void {
    for (const sock of this.pending.splice(0)) {
      writeReply(sock, { kind: "error", error: { code, message } }, 1);
    }
  }

  /** How many requests carrying `op` have arrived so far. */
  countOf(op: string): number {
    return this.requests.filter((r) => r.parsed.op === op).length;
  }

  async close(): Promise<void> {
    const server = this.server;
    this.server = null;
    for (const sock of this.pending.splice(0)) sock.destroy();
    for (const sock of this.sockets) sock.destroy();
    this.sockets.clear();
    if (!server) return;
    await new Promise<void>((resolve) => {
      server.close(() => resolve());
      server.closeAllConnections?.();
    });
  }
}

/** Create a tmpdir socket path and start a daemon on it. */
export async function startFakeDaemon(
  dir: string,
  socketName = "capwrap.sock",
): Promise<FakeDaemon> {
  const daemon = new FakeDaemon(`${dir}/${socketName}`);
  await daemon.listen();
  return daemon;
}

/** The parsed requests, for shape assertions. */
export function parsedRequests(daemon: FakeDaemon): WireRequest[] {
  return daemon.requests.map((r) => r.parsed);
}

/**
 * A socket path under `dir` that nothing ever listens on: connecting fails
 * immediately (ENOENT / ECONNREFUSED), which is the deterministic
 * "daemon unreachable" mode every shim must survive -- v1 by falling through
 * to its native question UI, opencode2 by honouring the policy `fallback`,
 * pi by blocking (fail-closed, because it has no native prompt).
 */
export function unreachableSocketPath(
  dir: string,
  socketName = "absent.sock",
): string {
  return `${dir}/${socketName}`;
}
