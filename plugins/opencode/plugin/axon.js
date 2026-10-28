// Axon activity adapter for opencode (in-process plugin, plain JS — no build step).
//
// Maps opencode hooks onto the shared reporter runtime exactly like the Pi
// adapter: identifiers and event names only go to hooks/run.py over stdin;
// raw prompts, tool args, and transcripts are never read. The per-session
// reporter command is appended to the system prompt via the experimental
// chat system transform. Every handler fails open: reporting must never
// break the agent.
//
// Settings come from the opencode process environment: AXON_BROKER,
// AXON_PORT, AXON_MACHINE, AXON_SOURCE, AXON_ACTIVITY_HOME, AXON_PYTHON.
import { spawn } from "node:child_process";
import { realpathSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

// Symlinked from ~/.config/opencode/plugin/, so resolve the real package.
const PACKAGE = dirname(dirname(realpathSync(fileURLToPath(import.meta.url))));
const RUNNER = process.env.AXON_PYTHON ?? "python3";
const SCRIPT = join(PACKAGE, "hooks", "run.py");

// Resolves to the hook's stdout (reporter context JSON); never rejects.
function send(payload, awaitOutput) {
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
        // The hook died early; reporting stays invisible.
      });
      child.stdin?.end(JSON.stringify(payload));
      if (!awaitOutput) child.unref();
    } catch {
      resolve("");
    }
  });
}

function contextOf(stdout) {
  try {
    const text = JSON.parse(stdout)?.hookSpecificOutput?.additionalContext;
    return typeof text === "string" ? text : "";
  } catch {
    return "";
  }
}

// Single named function export: opencode loads every exported plugin
// function from local plugin files (see @opencode-ai/plugin example.js).
// Exporting the same function under several names would double-register it.
export const AxonPlugin = async ({ directory }) => {
  // Constant per session, so the transformed system prompt stays cache-warm.
  const contexts = new Map();
  const started = new Set();
  const cwd = directory ?? process.cwd();
  const known = new Set();

  const base = (sessionID) => ({ session_id: String(sessionID ?? ""), cwd });

  return {
    async event({ event }) {
      try {
        const type = event?.type;
        const sessionID = event?.properties?.sessionID ?? event?.properties?.info?.sessionID ?? event?.properties?.info?.id;
        if (!sessionID) return;
        known.add(String(sessionID));
        if (type === "session.idle") {
          await send({ ...base(sessionID), hook_event_name: "Stop" }, false);
        } else if (type === "session.error") {
          await send({ ...base(sessionID), hook_event_name: "Interrupt" }, false);
        }
      } catch {
        // Never break the event bus.
      }
    },

    // A new user turn: report working (project/branch come from cwd) and
    // capture the session's reporting instructions for the system transform.
    async "chat.message"(input) {
      try {
        const sessionID = input?.sessionID;
        if (!sessionID) return;
        known.add(String(sessionID));
        if (!started.has(String(sessionID))) {
          started.add(String(sessionID));
          send({ ...base(sessionID), hook_event_name: "SessionStart" }, true).then((out) => {
            const text = contextOf(out);
            if (text) contexts.set(String(sessionID), text);
          });
        }
        const out = await send({ ...base(sessionID), hook_event_name: "UserPromptSubmit" }, true);
        const text = contextOf(out);
        if (text) contexts.set(String(sessionID), text);
      } catch {
        // Reporting failures never block a message.
      }
    },

    async "experimental.chat.system.transform"(input, output) {
      try {
        const text = contexts.get(String(input?.sessionID ?? ""));
        if (text && Array.isArray(output?.system) && !output.system.includes(text)) {
          output.system.push(text);
        }
      } catch {
        // Leave the system prompt untouched.
      }
    },

    async "tool.execute.before"(input) {
      send({ ...base(input?.sessionID), hook_event_name: "PreToolUse", tool_name: input?.tool ?? "" }, false);
    },

    async "tool.execute.after"(input) {
      send({ ...base(input?.sessionID), hook_event_name: "PostToolUse", tool_name: input?.tool ?? "" }, false);
    },

    // Gate hook: output.status is deliberately untouched; this only observes.
    async "permission.ask"(input) {
      send({ ...base(input?.sessionID), hook_event_name: "PermissionRequest" }, false);
    },

    // Plugin teardown: report the sessions this instance saw as ended.
    async dispose() {
      for (const sessionID of known) {
        send({ session_id: sessionID, cwd, hook_event_name: "SessionEnd" }, false);
      }
    },
  };
};

