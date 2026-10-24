import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { CONNECTION, FakeEventSource, stubServer } from "./test/fakes";

const RUN = "20261024-140000-c0ffee";
const DONE = "20261024-090000-beef00";
const connect = () => Promise.resolve(CONNECTION);

const SPEC = "# Add a token-bucket rate limiter\n\n- **10** requests a second\n\n| Limit | Value |\n|---|---|\n| burst | 20 |\n\n<script>alert('x')</script>\n<b>raw</b>\n";

async function openApp(files: Record<string, string> = {}) {
  const fetch = stubServer(files);
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
