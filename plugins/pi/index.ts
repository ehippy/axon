/**
 * Axon activity adapter for the Pi coding agent (pi.dev).
 *
 * Maps Pi lifecycle events onto the shared reporter runtime, exactly like the
 * Claude Code and Codex adapters, but in-process: Pi loads this module via
 * jiti, so every handler must fail open and never block the agent. Lifecycle
 * facts go to hooks/run.py over stdin (identifiers and event names only; raw
 * prompts, tool input, and transcripts are never read). The reporter command
 * for this session is injected into the system prompt on user-initiated runs.
 *
 * Configuration comes from the Pi process environment: AXON_BROKER, AXON_PORT,
 * AXON_MACHINE, AXON_SOURCE, AXON_ACTIVITY_HOME, optional AXON_PYTHON.
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { spawn } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const RUNNER = process.env.AXON_PYTHON ?? "python3";
const SCRIPT = join(dirname(fileURLToPath(import.meta.url)), "hooks", "run.py");

// Resolves to the hook's stdout (reporter context JSON); never rejects.
function send(payload: Record<string, unknown>, awaitOutput: boolean): Promise<string> {
  return new Promise((resolve) => {
    try {
      const child = spawn(RUNNER, [SCRIPT], { stdio: ["pipe", "pipe", "ignore"] });
      let out = "";
      const settle = () => {
        clearTimeout(timer);
        resolve("");
      };
      const timer = setTimeout(() => {
        try {
          child.kill();
        } catch {
          // The reporter is already gone; nothing to clean up.
        }
        resolve(out);
      }, 2500);
      child.stdout?.on("data", (chunk) => {
        out += String(chunk);
      });
      child.on("error", settle);
      child.on("close", () => {
        clearTimeout(timer);
        resolve(awaitOutput ? out : "");
      });
      child.stdin?.on("error", () => {
        // The hook died early; reporting must stay invisible.
      });
      child.stdin?.end(JSON.stringify(payload));
      if (!awaitOutput) child.unref();
    } catch {
      resolve("");
    }
  });
}

function contextOf(stdout: string): string {
  try {
    const parsed = JSON.parse(stdout);
    const text = parsed?.hookSpecificOutput?.additionalContext;
    return typeof text === "string" ? text : "";
  } catch {
    return "";
  }
}

export default function axonActivity(pi: ExtensionAPI) {
  // Constant per session, so the forced system prompt stays cache-warm.
  let activityContext = "";

  // Read identifiers synchronously; the session may be gone when the child runs.
  const base = (ctx: any): Record<string, unknown> => {
    let sessionId = "";
    try {
      sessionId = String(ctx?.sessionManager?.getSessionId?.() ?? "");
    } catch {
      sessionId = "";
    }
    return { session_id: sessionId, cwd: typeof ctx?.cwd === "string" ? ctx.cwd : "" };
  };

  pi.on("session_start", (_event, ctx) => {
    void send({ ...base(ctx), hook_event_name: "SessionStart" }, true).then((out) => {
      const text = contextOf(out);
      if (text) activityContext = text;
    });
  });

  // The gate event for a user-initiated run: report the new turn and append
  // the session's reporting instructions to the chained system prompt.
  pi.on("before_agent_start", async (event: any, ctx) => {
    if (event?.messageType !== "user") return undefined;
    const out = await send({ ...base(ctx), hook_event_name: "UserPromptSubmit" }, true);
    const text = contextOf(out) || activityContext;
    if (!text) return undefined;
    activityContext = text;
    return { systemPrompt: `${event.systemPrompt}\n\n${text}` };
  });

  pi.on("tool_call", (event: any, ctx) => {
    void send({ ...base(ctx), hook_event_name: "PreToolUse", tool_name: event?.toolName ?? "" }, false);
  });

  // Every finalized call gets this, including blocked and failed ones.
  pi.on("tool_execution_end", (event: any, ctx) => {
    void send({ ...base(ctx), hook_event_name: "PostToolUse", tool_name: event?.toolName ?? "" }, false);
  });

  // Notification-only waiting span around confirm/select/input prompts.
  pi.on("ui_prompt_start", (_event, ctx) => {
    void send({ ...base(ctx), hook_event_name: "PermissionRequest" }, false);
  });

  // agent_settled is the final idle signal; agent_end may still continue.
  pi.on("agent_settled", (event: any, ctx) => {
    void send({ ...base(ctx), hook_event_name: event?.aborted ? "Interrupt" : "Stop" }, false);
  });

  // A reload re-emits session_start for the same session; keep its history.
  pi.on("session_shutdown", (event: any, ctx) => {
    if (event?.reason !== "reload") void send({ ...base(ctx), hook_event_name: "SessionEnd" }, false);
  });
}
