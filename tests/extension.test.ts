import { expect, test } from "bun:test";
import type { ExtensionAPI, ExtensionCommandContext } from "@oh-my-pi/pi-coding-agent";
import ompup from "../extension/index.ts";


test("bare command defaults to handoff and refuses running jobs", async () => {
  type CommandHandler = (args: string, ctx: ExtensionCommandContext) => Promise<void> | void;
  let handler: CommandHandler | undefined;
  const notifications: Array<{ message: string; level: string }> = [];
  const api = {
    setLabel() { },
    registerCommand(_name: string, options: { handler: CommandHandler }) {
      handler = options.handler;
    },
  };
  // Minimal in-process test double for the registration surface used here.
  ompup(api as unknown as ExtensionAPI);

  if (!handler) throw new Error("ompup command was not registered");
  const context = {
    ui: {
      notify(message: string, level: string) {
        notifications.push({ message, level });
      },
    },
    async waitForIdle() { },
    getAsyncJobSnapshot() {
      return {
        running: [{ id: "bash_123", label: "long build" }],
        recent: [],
        delivery: {},
      };
    },
    sessionManager: {
      getSessionFile() {
        throw new Error("session transfer must not begin");
      },
    },
  } as unknown as ExtensionCommandContext;
  await handler("", context);

  expect(notifications).toEqual([
    {
      message: "Wait for or cancel active background jobs before handoff: long build",
      level: "error",
    },
  ]);
});
