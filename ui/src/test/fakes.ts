import { vi } from "vitest";
import recorded from "./recorded-stream.json";

/** The events `orchestrator.py serve` sent over a scripted run, recorded off a real server: each batch is what one
 * poll sent after the step its `after` names. The runs are 20261024-090000-beef00, finished and approved with a pull
 * request when the stream opens, and 20261024-140000-c0ffee, which goes from its interview to the review, blocking
 * once, and whose spec.md is edited after. */
export const RECORDED: { after: string; events: ({ kind: string } & Record<string, unknown>)[] }[] = recorded;

export const CONNECTION = { url: "http://127.0.0.1:4242", token: "s3cret", project: "/home/me/proj" };

/** An EventSource the test drives; every one the app opens is in `opened`. */
export class FakeEventSource {
  static opened: FakeEventSource[] = [];
  closed = false;
  private listeners = new Map<string, ((event: MessageEvent<string>) => void)[]>();

  constructor(readonly url: string) {
    FakeEventSource.opened.push(this);
  }

  static get last(): FakeEventSource {
    const source = FakeEventSource.opened.at(-1);
    if (!source) {
      throw new Error("no EventSource was opened");
    }
    return source;
  }

  addEventListener(kind: string, listener: (event: MessageEvent<string>) => void): void {
    this.listeners.set(kind, [...(this.listeners.get(kind) ?? []), listener]);
  }

  close(): void {
    this.closed = true;
  }

  /** Dispatch an event of the kind, with data as its JSON; a bare open or error without it. */
  emit(kind: string, data?: unknown): void {
    const event = new MessageEvent(kind, { data: data === undefined ? undefined : JSON.stringify(data) });
    for (const listener of this.listeners.get(kind) ?? []) {
      listener(event);
    }
  }

  /** Dispatch the recorded batches up to and including the one after the step `through` names. */
  replay(through: string): void {
    const last = RECORDED.findIndex((batch) => batch.after === through);
    if (last < 0) {
      throw new Error(`no recorded batch after ${through}`);
    }
    for (const batch of RECORDED.slice(0, last + 1)) {
      this.replayBatch(batch);
    }
  }

  /** Dispatch the one recorded batch after the step `after` names. */
  replayOne(after: string): void {
    const batch = RECORDED.find((b) => b.after === after);
    if (!batch) {
      throw new Error(`no recorded batch after ${after}`);
    }
    this.replayBatch(batch);
  }

  private replayBatch(batch: (typeof RECORDED)[number]): void {
    for (const { kind, ...data } of batch.events) {
      this.emit(kind, data);
    }
  }
}

/** Stub EventSource, and fetch with a server whose /file answers the text of `files` by name, else 404; whose /agent
 * answers the screen of `agents` by role, else 404 as for an agent that has exited; and whose POST /agent/input
 * takes any input. */
export function stubServer(files: Record<string, string>, agents: Record<string, string> = {}) {
  FakeEventSource.opened = [];
  vi.stubGlobal("EventSource", FakeEventSource);
  const answer = (status: number, body: unknown) => new Response(JSON.stringify(body), { status });
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    const { pathname, searchParams } = new URL(url);
    const role = searchParams.get("role") ?? "";
    if (pathname === "/agent/input") {
      return init?.method === "POST" ? answer(200, { run: "r", role }) : answer(501, {});
    }
    if (pathname === "/agent") {
      const text = agents[role];
      return text === undefined
        ? answer(404, { error: `the ${role} agent has exited` })
        : answer(200, { run: "r", role, text });
    }
    const name = searchParams.get("name") ?? "";
    const text = files[name];
    return text === undefined ? answer(404, { error: `no file ${name}` }) : answer(200, { run: "r", name, text });
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}
