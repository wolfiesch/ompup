/**
 * ompup omp extension.
 *
 * Adds a /ompup slash command so you can synchronize the current project with
 * its selected remote machine without leaving the session:
 *
 *   /ompup sync      safely transfer local uncommitted state to the remote
 *   /ompup pull      safely transfer remote uncommitted state to this checkout
 *   /ompup status    compare Git, synchronization, and tmux state
 *   /ompup handoff   resume this exact session inside remote tmux
 *
 * Handoff waits for the active turn to finish, transfers and remotely validates
 * the persisted session plus artifacts, then replaces the calling cmux surface.
 * Failures leave the local OMP process and session file untouched.
 */
import { execFile } from "node:child_process";
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent";

const SUBCOMMANDS = ["sync", "pull", "status", "handoff"] as const;

function resolveSubcommand(token: string): string | null {
 const matches = SUBCOMMANDS.filter((name) => name.startsWith(token));
 return matches.length === 1 ? matches[0] : null;
}

function cliPath(): string {
 // Prefer the CLI bundled beside this module (plugin/link installs keep
 // the repo layout); fall back to `ompup` on PATH.
 const bundled = fileURLToPath(new URL("../bin/ompup", import.meta.url));
 return existsSync(bundled) ? bundled : "ompup";
}

function run(
 bin: string,
 args: string[],
 cwd: string,
 timeout: number = 120_000,
): Promise<{ ok: boolean; output: string }> {
 const { promise, resolve } = Promise.withResolvers<{ ok: boolean; output: string }>();
 execFile(bin, args, { cwd, timeout }, (error, stdout, stderr) => {
  let output = `${stdout}${stderr}`.trim();
  // A missing or non-executable CLI produces no stdout/stderr; surface the
  // spawn error instead of a bare "failed" notification.
  if (error && !output) output = error.message;
  resolve({ ok: error === null, output });
 });
 return promise;
}

function parseHandoffOptions(tokens: string[]): string[] | null {
 const result: string[] = [];
 for (let index = 0; index < tokens.length; index += 2) {
  const option = tokens[index];
  const value = tokens[index + 1];
  if (!["--host", "--profile"].includes(option) || !value || value.startsWith("-")) {
   return null;
  }
  result.push(option, value);
 }
 return result;
}

export default function ompup(pi: ExtensionAPI) {
 pi.setLabel("ompup");

 pi.registerCommand("ompup", {
  description: "Hand this live session to remote tmux, or sync project state",
  handler: async (args, ctx) => {
   const tokens = args.trim() ? args.trim().split(/\s+/) : ["handoff"];
   const sub = resolveSubcommand(tokens.shift()!);
   if (!sub) {
    ctx.ui.notify(`/ompup takes one of: ${SUBCOMMANDS.join(", ")} (a unique prefix works)`, "error");
    return;
   }

   const bin = cliPath();
   if (sub === "handoff") {
    const options = parseHandoffOptions(tokens);
    if (!options) {
     ctx.ui.notify("/ompup handoff accepts only --host NAME and --profile NAME", "error");
     return;
    }
    await ctx.waitForIdle();
    const runningJobs = ctx.getAsyncJobSnapshot()?.running ?? [];
    if (runningJobs.length) {
     const jobs = runningJobs.map((job) => job.label || job.id).join(", ");
     ctx.ui.notify(`Wait for or cancel active background jobs before handoff: ${jobs}`, "error");
     return;
    }
    const sessionFile = ctx.sessionManager.getSessionFile();
    if (!sessionFile) {
     ctx.ui.notify("The active session is not persisted yet; send one message before handoff", "error");
     return;
    }
    const artifactsDir = ctx.sessionManager.getArtifactsDir();
    const commandArgs = [
     "handoff",
     ...options,
     "--session-file",
     sessionFile,
     "--session-id",
     ctx.sessionManager.getSessionId(),
    ];
    if (artifactsDir) commandArgs.push("--artifacts-dir", artifactsDir);
    const { ok, output } = await run(bin, commandArgs, ctx.cwd, 300_000);
    const summary = output.length > 500 ? `...${output.slice(-500)}` : output;
    if (!ok) {
     ctx.ui.notify(summary || "ompup handoff failed; local session preserved", "error");
     return;
    }
    ctx.ui.notify(summary || "ompup handoff complete", "info");
    ctx.shutdown();
    return;
   }

   if (tokens.length) {
    ctx.ui.notify(`/ompup ${sub} takes no additional arguments`, "error");
    return;
   }
   const timeout = sub === "status" ? 120_000 : 300_000;
   const { ok, output } = await run(bin, [sub], ctx.cwd, timeout);
   const summary = output.length > 400 ? `${output.slice(0, 400)}...` : output;
   ctx.ui.notify(
    summary || (ok ? `ompup ${sub}: done` : `ompup ${sub}: failed`),
    ok ? "info" : "error",
   );
  },
 });
}
