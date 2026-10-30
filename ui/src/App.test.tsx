import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { AGENT_POLL_MS } from "./components/AgentPanes";
import { CONNECTION, FakeEventSource, RECORDED, stubServer } from "./test/fakes";

const RUN = "20261024-140000-c0ffee";
const DONE = "20261024-090000-beef00";
const connect = () => Promise.resolve(CONNECTION);

const SPEC = "# Add a token-bucket rate limiter\n\n- **10** requests a second\n\n| Limit | Value |\n|---|---|\n| burst | 20 |\n\n<script>alert('x')</script>\n<b>raw</b>\n";

async function openApp(files: Record<string, string> = {}, agents: Record<string, string> = {}) {
  const fetch = stubServer(files, agents);
  render(<App connect={connect} />);
  // The stream opens in an effect, which may run after the first render shows.
  await waitFor(() => expect(FakeEventSource.opened).toHaveLength(1));
  return { fetch, source: FakeEventSource.last };
}

function row(run: string): HTMLElement {
  const element = document.querySelector<HTMLElement>(`tr[data-run="${run}"]`);
  if (!element) {
    throw new Error(`no row for ${run}`);
  }
  return element;
}

function stages(container: HTMLElement): [string | null, string | null][] {
  return [...container.querySelectorAll("li.stage")].map((li) => [li.textContent, li.getAttribute("data-state")]);
}

describe("the runs overview", () => {
  it("opens the checkout's project on the server's stream, with the token", async () => {
    const { source } = await openApp();
    const url = new URL(source.url);
    expect(`${url.origin}${url.pathname}`).toBe("http://127.0.0.1:4242/events");
    expect(url.searchParams.get("cwd")).toBe("/home/me/proj");
    expect(url.searchParams.get("token")).toBe("s3cret");
    expect(url.searchParams.has("machine")).toBe(false);
    expect(screen.getByText("No runs.")).toBeInTheDocument();
  });

  it("follows a run through its phases as the events arrive", async () => {
    const { source } = await openApp();
    act(() => source.replay("the stream opened"));
    const done = row(DONE);
    expect(within(done).getByText("APPROVE")).toBeInTheDocument();
    expect(within(done).getByRole("link", { name: "#7" })).toHaveAttribute("href", "https://github.com/o/r/pull/7");
    expect(stages(done)).toEqual([["spec", "done"], ["build", "done"], ["review", "done"]]);

    act(() => source.replayOne("a run started its interview"));
    expect(stages(row(RUN))).toEqual([["spec", "current"], ["build", "todo"], ["review", "todo"]]);
    expect(within(row(RUN)).getByText("round 0")).toBeInTheDocument();
    expect(within(row(RUN)).getByText(/^running, beat \d+s ago$/)).toBeInTheDocument();
    // Newest first.
    expect([...document.querySelectorAll("tbody tr")].map((tr) => tr.getAttribute("data-run"))).toEqual([RUN, DONE]);

    act(() => source.replayOne("the spec was written and the build started"));
    expect(stages(row(RUN))).toEqual([["spec", "done"], ["build", "current"], ["review", "todo"]]);
    expect(within(row(RUN)).getByText("round 1")).toBeInTheDocument();
    expect(row(RUN).querySelector(".role")).toHaveTextContent(/^build$/);

    act(() => source.replayOne("the Builder blocked"));
    expect(within(row(RUN)).getByText("blocked: should the limit be per client or global?")).toBeInTheDocument();

    act(() => source.replayOne("the build was reported and the review started"));
    expect(stages(row(RUN))).toEqual([["spec", "done"], ["build", "done"], ["review", "current"]]);
    expect(within(row(RUN)).queryByText(/blocked/)).not.toBeInTheDocument();

    act(() => source.replay("the finished run was deleted"));
    expect(document.querySelector(`tr[data-run="${DONE}"]`)).toBeNull();
    expect(row(RUN)).toBeInTheDocument();
  });

  it("ages the heartbeat between events", async () => {
    // Frozen time, which moves only when advanced: findBy's polling would need it to move by itself.
    vi.useFakeTimers();
    stubServer({});
    render(<App connect={connect} />);
    await act(async () => {});
    const source = FakeEventSource.last;
    act(() => source.replay("a run started its interview"));
    const age = () => Number(within(row(RUN)).getByText(/^running, beat/).textContent?.match(/(\d+)s/)?.[1]);
    const before = age();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    expect(age()).toBe(before + 3);
  });

  it("shows the server's problem until it clears", async () => {
    const { source } = await openApp();
    act(() => source.emit("problem", { error: "ssh: connection refused" }));
    expect(screen.getByText("ssh: connection refused")).toBeInTheDocument();
    act(() => source.emit("problem", { error: null }));
    expect(screen.queryByText("ssh: connection refused")).not.toBeInTheDocument();
  });

  it("starts from no runs when the stream reconnects", async () => {
    const { source } = await openApp();
    act(() => source.replay("the stream opened"));
    act(() => source.emit("open"));
    expect(screen.getByText("No runs.")).toBeInTheDocument();
  });

  it("opens another project, on a machine, and remembers it", async () => {
    const { source } = await openApp();
    fireEvent.change(screen.getByLabelText("Project folder"), { target: { value: " ~/other " } });
    fireEvent.change(screen.getByLabelText("Machine"), { target: { value: "devbox" } });
    fireEvent.click(screen.getByRole("button", { name: "Open" }));
    expect(source.closed).toBe(true);
    const url = new URL(FakeEventSource.last.url);
    expect(url.searchParams.get("cwd")).toBe("~/other");
    expect(url.searchParams.get("machine")).toBe("devbox");
    expect(JSON.parse(localStorage.getItem("orchestrator.project") ?? "")).toEqual({ cwd: "~/other", machine: "devbox" });
  });

  it("asks for a project when the orchestrator belongs to no checkout", async () => {
    stubServer({});
    render(<App connect={() => Promise.resolve({ ...CONNECTION, project: null })} />);
    expect(await screen.findByText("Open a project folder to see its runs.")).toBeInTheDocument();
    expect(FakeEventSource.opened).toHaveLength(0);
    fireEvent.change(screen.getByLabelText("Project folder"), { target: { value: "~/proj" } });
    fireEvent.click(screen.getByRole("button", { name: "Open" }));
    expect(new URL(FakeEventSource.last.url).searchParams.get("cwd")).toBe("~/proj");
  });

  it("says why the orchestrator could not start", async () => {
    render(<App connect={() => Promise.reject(new Error("python3: not found"))} />);
    expect(await screen.findByText(/Could not start the orchestrator: Error: python3: not found/)).toBeInTheDocument();
  });
});

describe("the run detail", () => {
  it("renders the newest handoff file as Markdown, without its raw HTML", async () => {
    const { fetch, source } = await openApp({ "spec.md": SPEC });
    act(() => source.replay("the spec was written and the build started"));
    fireEvent.click(row(RUN));
    const detail = screen.getByRole("region", { name: "run c0ffee" });
    expect(stages(detail)).toEqual([["spec", "done"], ["build", "current"], ["review", "todo"]]);
    expect(await within(detail).findByRole("heading", { name: "Add a token-bucket rate limiter" })).toBeInTheDocument();
    expect(within(detail).getByText("10").tagName).toBe("STRONG");
    expect(within(detail).getByRole("cell", { name: "20" })).toBeInTheDocument();
    expect(detail.querySelector("article script, article b")).toBeNull();
    expect(within(detail).queryByText(/alert/)).not.toBeInTheDocument();
    const [url, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(new URL(url).searchParams.get("name")).toBe("spec.md");
    expect(new URL(url).searchParams.get("run")).toBe(RUN);
    expect(init.headers).toEqual({ Authorization: "Bearer s3cret" });
  });

  it("lists new files as they appear and renders the chosen one again when it changes", async () => {
    const files = { "spec.md": SPEC, "build-1.blocked-1.md": "BLOCKED: per client?\n", "build-1.md": "# Build 1\n" };
    const { fetch, source } = await openApp(files);
    act(() => source.replay("the spec was written and the build started"));
    fireEvent.click(row(RUN));
    const detail = screen.getByRole("region", { name: "run c0ffee" });
    const names = () => within(within(detail).getByRole("list", { name: "files" })).getAllByRole("button").map((b) => b.firstChild?.textContent);
    expect(names()).toEqual(["spec.md"]);
    await within(detail).findByRole("heading", { name: "Add a token-bucket rate limiter" });
    fireEvent.click(within(detail).getByRole("button", { name: /spec\.md/ }));

    act(() => source.replay("the build was reported and the review started"));
    expect(names()).toEqual(["spec.md", "build-1.blocked-1.md", "build-1.md"]);
    expect(stages(detail)).toEqual([["spec", "done"], ["build", "done"], ["review", "current"]]);
    // The chosen file stays, and is not fetched again while it is unchanged.
    expect(within(detail).getByRole("heading", { name: "Add a token-bucket rate limiter" })).toBeInTheDocument();
    const fetches = fetch.mock.calls.length;

    files["spec.md"] = "# Add a token-bucket rate limiter\n\n- **20** requests a second\n";
    act(() => source.replayOne("the spec was edited"));
    expect(await within(detail).findByText("20")).toBeInTheDocument();
    expect(fetch.mock.calls.length).toBe(fetches + 1);

    fireEvent.click(within(detail).getByRole("button", { name: /build-1\.md/ }));
    expect(await within(detail).findByRole("heading", { name: "Build 1" })).toBeInTheDocument();
  });

  it("says when a file cannot be read", async () => {
    const { source } = await openApp({});
    act(() => source.replay("the spec was written and the build started"));
    fireEvent.click(row(RUN));
    expect(await screen.findByText("no file spec.md")).toBeInTheDocument();
  });

  it("goes back to the overview", async () => {
    const { source } = await openApp({ "spec.md": SPEC });
    act(() => source.replay("the spec was written and the build started"));
    fireEvent.click(row(RUN));
    fireEvent.click(screen.getByRole("button", { name: "‹ Runs" }));
    expect(row(RUN)).toBeInTheDocument();
  });

  it("goes back to the overview when its run is deleted", async () => {
    const { source } = await openApp({ "review-1.md": "VERDICT: APPROVE\n" });
    act(() => source.replay("the stream opened"));
    fireEvent.click(row(DONE));
    expect(screen.getByRole("region", { name: "run beef00" })).toBeInTheDocument();
    act(() => source.replay("the finished run was deleted"));
    expect(screen.queryByRole("region", { name: "run beef00" })).not.toBeInTheDocument();
  });
});

describe("the agents", () => {
  const AGENTS = [
    { role: "spec", name: "spec-c0ffee", kind: "claude" },
    { role: "build", name: "build-c0ffee", kind: "codex" },
  ];

  /** Open the app on the run in its build, with the agents above, and its detail. */
  async function openAgents(screens: Record<string, string>) {
    const opened = await openApp({ "spec.md": SPEC }, screens);
    act(() => opened.source.replay("the spec was written and the build started"));
    const batch = RECORDED.find((b) => b.after === "the spec was written and the build started");
    const record = batch?.events.find((e) => e.kind === "record")?.record as Record<string, unknown>;
    act(() => opened.source.emit("record", { run: RUN, record: { ...record, agents: AGENTS } }));
    fireEvent.click(row(RUN));
    return { ...opened, agents: screen.getByRole("region", { name: "agents" }) };
  }

  function agentCalls(fetch: ReturnType<typeof stubServer>) {
    return fetch.mock.calls
      .map(([url, init]) => [new URL(url as string), init as RequestInit | undefined] as const)
      .filter(([url]) => url.pathname.startsWith("/agent"));
  }

  it("shows the current role's agent first, and another when its tab is chosen", async () => {
    const { fetch, agents } = await openAgents({ build: "> building the limiter\n", spec: "> spec written\n" });
    expect(within(agents).getAllByRole("tab").map((t) => t.textContent)).toEqual(["spec", "build"]);
    expect(within(agents).getByRole("tab", { name: "build" })).toHaveAttribute("aria-selected", "true");
    expect(await within(agents).findByText("> building the limiter")).toBeInTheDocument();
    const [url, init] = agentCalls(fetch)[0] ?? [];
    const asked = [url?.pathname, url?.searchParams.get("run"), url?.searchParams.get("role"), url?.searchParams.get("lines")];
    expect(asked).toEqual(["/agent", RUN, "build", "200"]);
    expect(init?.headers).toEqual({ Authorization: "Bearer s3cret" });

    fireEvent.click(within(agents).getByRole("tab", { name: "spec" }));
    expect(await within(agents).findByText("> spec written")).toBeInTheDocument();
  });

  it("reads the shown agent again while it is shown", async () => {
    vi.useFakeTimers();
    const fetch = stubServer({ "spec.md": SPEC }, { build: "> one\n" });
    render(<App connect={connect} />);
    await act(async () => {});
    const source = FakeEventSource.last;
    act(() => source.replay("the spec was written and the build started"));
    const batch = RECORDED.find((b) => b.after === "the spec was written and the build started");
    const record = batch?.events.find((e) => e.kind === "record")?.record as Record<string, unknown>;
    act(() => source.emit("record", { run: RUN, record: { ...record, agents: AGENTS } }));
    fireEvent.click(row(RUN));
    await act(async () => {});
    const reads = () => agentCalls(fetch).length;
    expect(reads()).toBe(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(reads()).toBe(2);
    fireEvent.click(screen.getByRole("button", { name: "‹ Runs" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10000);
    });
    expect(reads()).toBe(2);
  });

  it("types a message and keys into the agent", async () => {
    const { fetch, agents } = await openAgents({ build: "> which limit?\n" });
    await within(agents).findByText("> which limit?");
    const box = within(agents).getByLabelText("Message to the build agent");
    fireEvent.change(box, { target: { value: "per client" } });
    fireEvent.click(within(agents).getByRole("button", { name: "Send" }));
    await waitFor(() => expect(box).toHaveValue(""));
    fireEvent.click(within(agents).getByRole("button", { name: "press esc" }));
    await waitFor(() => expect(agentCalls(fetch).filter(([, init]) => init?.method === "POST")).toHaveLength(2));
    const posts = agentCalls(fetch).filter(([, init]) => init?.method === "POST");
    expect(posts.map(([url, init]) => [url.pathname, url.searchParams.get("role"), init?.body])).toEqual([
      ["/agent/input", "build", JSON.stringify({ text: "per client" })],
      ["/agent/input", "build", JSON.stringify({ keys: ["esc"] })],
    ]);
  });

  it("sends a message once while it is being sent, and keeps it when the send fails", async () => {
    const { fetch, agents } = await openAgents({ build: "> which limit?\n" });
    await within(agents).findByText("> which limit?");
    // Hold each send until the test answers it.
    const serve = fetch.getMockImplementation();
    const answers: ((response: Response) => void)[] = [];
    fetch.mockImplementation((url: string, init?: RequestInit) =>
      new URL(url).pathname === "/agent/input"
        ? new Promise<Response>((resolve) => answers.push(resolve))
        : (serve?.(url, init) ?? Promise.reject(new Error("no server"))),
    );
    const posts = () => agentCalls(fetch).filter(([, init]) => init?.method === "POST");
    const box = within(agents).getByLabelText("Message to the build agent");
    const send = within(agents).getByRole("button", { name: "Send" });
    fireEvent.change(box, { target: { value: "per client" } });
    fireEvent.submit(box);
    fireEvent.submit(box);
    fireEvent.click(send);
    expect(posts()).toHaveLength(1);
    expect(send).toBeDisabled();

    await act(async () => answers[0]?.(new Response(JSON.stringify({ error: "herdr: no such pane" }), { status: 502 })));
    expect(await within(agents).findByText("herdr: no such pane")).toBeInTheDocument();
    expect(box).toHaveValue("per client");
    expect(send).toBeEnabled();

    fireEvent.submit(box);
    expect(posts()).toHaveLength(2);
    await act(async () => answers[1]?.(new Response(JSON.stringify({ run: "r", role: "build" }), { status: 200 })));
    await waitFor(() => expect(box).toHaveValue(""));
  });

  it("stops reading an agent that has exited, until a send", async () => {
    vi.useFakeTimers();
    const fetch = stubServer({ "spec.md": SPEC }, {});
    render(<App connect={connect} />);
    await act(async () => {});
    const source = FakeEventSource.last;
    act(() => source.replay("the spec was written and the build started"));
    const batch = RECORDED.find((b) => b.after === "the spec was written and the build started");
    const record = batch?.events.find((e) => e.kind === "record")?.record as Record<string, unknown>;
    act(() => source.emit("record", { run: RUN, record: { ...record, agents: AGENTS } }));
    fireEvent.click(row(RUN));
    await act(async () => {});
    const reads = () => agentCalls(fetch).filter(([url]) => url.pathname === "/agent").length;
    expect(screen.getByText("the build agent has exited")).toBeInTheDocument();
    expect(reads()).toBe(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10 * AGENT_POLL_MS);
    });
    expect(reads()).toBe(1);

    fireEvent.click(screen.getByRole("button", { name: "press esc" }));
    await act(async () => {});
    expect(reads()).toBe(2);
  });

  it("says when an agent has exited, and when none has started", async () => {
    const { agents } = await openAgents({});
    expect(await within(agents).findByText("the build agent has exited")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "‹ Runs" }));
    fireEvent.click(row(DONE));
    expect(within(screen.getByRole("region", { name: "agents" })).getByText("No agent has started yet.")).toBeInTheDocument();
  });
});
