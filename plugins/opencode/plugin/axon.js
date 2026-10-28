// Axon activity adapter for opencode (in-process plugin, plain JS — no build step).
//
// Dual-shape plugin per https://opencode.ai/v2/docs/build/plugins/server:
// the default export carries both the v2 {id, setup} definition and a v1
// server() hook factory, so one file works on OpenCode >= 1.18.29 and on v2.
//
// Identifiers and event names only go to hooks/run.py over stdin; raw
// prompts, tool args, and transcripts are never read. Every handler fails
// open: reporting must never break the agent.
//
// Settings come from the opencode process environment: AXON_BROKER,
// AXON_PORT, AXON_MACHINE, AXON_SOURCE, AXON_ACTIVITY_HOME, AXON_PYTHON.
import { spawn } from "node:child_process";
import { realpathSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

// Installed as a symlink into ~/.config/opencode/plugins/, so resolve the
// real package to find the hook runner.
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

// Per-session bookkeeping shared by the v1 and v2 surfaces (only one runs).
function makeTracker(cwd) {
  const contexts = new Map();
  const started = new Set();
  const known = new Set();
  const base = (sessionID) => ({ session_id: String(sessionID ?? ""), cwd });

  return {
    contexts,
    base,
    track(sessionID) {
      if (!sessionID) return false;
      known.add(String(sessionID));
      return true;
    },
    async firstTurn(sessionID) {
      if (!started.has(String(sessionID))) {
        started.add(String(sessionID));
        const out = await send({ ...base(sessionID), hook_event_name: "SessionStart" }, true);
        const text = contextOf(out);
        if (text) contexts.set(String(sessionID), text);
      }
    },
    async userTurn(sessionID) {
      const out = await send({ ...base(sessionID), hook_event_name: "UserPromptSubmit" }, true);
      const text = contextOf(out);
      if (text) contexts.set(String(sessionID), text);
    },
    endAll() {
      for (const sessionID of known) {
        send({ session_id: sessionID, cwd, hook_event_name: "SessionEnd" }, false);
      }
    },
  };
}

// Reporter text goes in as a system part; clone the trailing part's shape so
// provider-specific keys survive, and never append the same text twice.
function appendSystem(system, text) {
  if (!Array.isArray(system) || !text) return;
  const last = system[system.length - 1];
  if (typeof last?.text === "string" && last.text.includes(text)) return;
  system.push(last ? { ...last, text } : { text });
}

// OpenCode v2: default export with id + setup.
export default {
  id: "axon-activity",

  async setup(ctx) {
    const cwd = ctx?.location?.directory ?? process.cwd();
    const tracker = makeTracker(cwd);
    let alive = true;

    // A new user turn: SessionStart once, then UserPromptSubmit per turn.
    await ctx.session.hook("prompt", async (event) => {
      try {
        if (!tracker.track(event?.sessionID)) return;
        await tracker.firstTurn(event.sessionID);
        await tracker.userTurn(event.sessionID);
      } catch {
        // Reporting failures never block a prompt.
      }
    });

    // Append the reporter command to the system prompt (constant per
    // session, so prompt caching stays warm).
    await ctx.session.hook("context", async (event) => {
      try {
        appendSystem(event?.system, tracker.contexts.get(String(event?.sessionID ?? "")));
      } catch {
        // Leave the system prompt untouched.
      }
    });

    await ctx.tool.hook("execute.before", (event) => {
      if (!tracker.track(event?.sessionID)) return;
      send({ ...tracker.base(event?.sessionID), hook_event_name: "PreToolUse", tool_name: event?.tool ?? "" }, false);
    });

    await ctx.tool.hook("execute.after", (event) => {
      if (!tracker.track(event?.sessionID)) return;
      send({ ...tracker.base(event?.sessionID), hook_event_name: "PostToolUse", tool_name: event?.tool ?? "" }, false);
    });

    // Observation only: effect/message are deliberately never touched.
    await ctx.permission.hook("evaluate", (event) => {
      if (!tracker.track(event?.sessionID)) return;
      send({ ...tracker.base(event?.sessionID), hook_event_name: "PermissionRequest" }, false);
    });

    // Idle / error lifecycle from the event bus.
    (async () => {
      try {
        for await (const event of ctx.event.subscribe()) {
          if (!alive) return;
          try {
            const properties = event?.properties ?? event;
            const sessionID =
              properties?.sessionID ?? properties?.info?.sessionID ?? properties?.info?.id;
            if (!tracker.track(sessionID)) continue;
            if (event?.type === "session.idle") {
              send({ ...tracker.base(sessionID), hook_event_name: "Stop" }, false);
            } else if (event?.type === "session.error") {
              send({ ...tracker.base(sessionID), hook_event_name: "Interrupt" }, false);
            }
          } catch {
            // Never break the event bus.
          }
        }
      } catch {
        // Subscription ended; teardown handles the rest.
      }
    })();

    return async () => {
      alive = false;
      tracker.endAll();
    };
  },

  // OpenCode v1 (>= 1.18.29): server() returns the legacy hook map.
  async server() {
    const cwd = process.cwd();
    const tracker = makeTracker(cwd);

    return {
      async event({ event }) {
        try {
          const sessionID =
            event?.properties?.sessionID ?? event?.properties?.info?.sessionID ?? event?.properties?.info?.id;
          if (!tracker.track(sessionID)) return;
          if (event?.type === "session.idle") {
            await send({ ...tracker.base(sessionID), hook_event_name: "Stop" }, false);
          } else if (event?.type === "session.error") {
            await send({ ...tracker.base(sessionID), hook_event_name: "Interrupt" }, false);
          }
        } catch {
          // Never break the event bus.
        }
      },

      async "chat.message"(input) {
        try {
          if (!tracker.track(input?.sessionID)) return;
          await tracker.firstTurn(input.sessionID);
          await tracker.userTurn(input.sessionID);
        } catch {
          // Reporting failures never block a message.
        }
      },

      async "experimental.chat.system.transform"(input, output) {
        try {
          appendSystem(output?.system, tracker.contexts.get(String(input?.sessionID ?? "")));
        } catch {
          // Leave the system prompt untouched.
        }
      },

      async "tool.execute.before"(input) {
        if (!tracker.track(input?.sessionID)) return;
        send({ ...tracker.base(input?.sessionID), hook_event_name: "PreToolUse", tool_name: input?.tool ?? "" }, false);
      },

      async "tool.execute.after"(input) {
        if (!tracker.track(input?.sessionID)) return;
        send({ ...tracker.base(input?.sessionID), hook_event_name: "PostToolUse", tool_name: input?.tool ?? "" }, false);
      },

      // Gate hook: output.status is deliberately untouched; this only observes.
      async "permission.ask"(input) {
        if (!tracker.track(input?.sessionID)) return;
        send({ ...tracker.base(input?.sessionID), hook_event_name: "PermissionRequest" }, false);
      },

      async dispose() {
        tracker.endAll();
      },
    };
  },
};
