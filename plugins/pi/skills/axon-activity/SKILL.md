---
name: axon-activity
description: Report the goal, meaningful progress, blocker, or result of a Pi session to Axon, or inspect recorded agent activity. Use when Axon reporting is enabled or the user asks about agent work and history.
---

Use the session-specific reporter command supplied by the Axon reporting
instructions in your system prompt. Never guess a session ID, choose another
chat's ID, or substitute the most recently active session on the machine.

Report a semantic goal at the beginning of substantive work. Update significant
milestones and blockers; hooks already report generic tool activity. Before the
final answer, record an outcome and actual evidence only when the requested work
is complete. For pauses use waiting or blocked. A normal settled turn is not
proof that the task finished. Reporting failures must not derail the user's work.

Append these arguments to the supplied command:

- `start --project PROJECT --goal "Desired outcome" --step "Current meaningful step"`
- `progress --step "Progress or direction change"`
- `blocked --blocker "Specific dependency"`
- `waiting --step "Decision needed"`
- `finished --outcome "Concrete result" --evidence "Actual test, commit, PR or artifact"`

Keep secrets, raw prompts, transcripts and command arguments out of reports.
A report is an agent claim, not independently verified proof. Do not mark an
outcome finished just because tools stopped or the chat is ending.
