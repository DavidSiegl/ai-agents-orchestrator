import { describe, expect, it } from "vitest";
import type { RunRecord } from "./api";
import { applyEvent, formatAge, formatRound, heartbeatAge, NO_RUNS, outcome, prLabel, progress } from "./runs";

const STEPS = [
  { id: "spec", role: "spec" },
  { id: "build", role: "build" },
  { id: "review", role: "review" },
];

function record(overrides: Partial<RunRecord> = {}): RunRecord {
  return {
    id: "20261024-140000-c0ffee", key: "c0ffee", task: "t", workflow: "default", steps: STEPS, phase: "build",
    round: 1, quality_round: null, role: "build", pane: null, health: "running", health_reason: "beat 4s ago",
    heartbeat_age: 4, waiting: null, verdict: null, error: null, closed: null, branch: null, worktree: null, pr: null,
    turns: [], resume_command: null, ...overrides,
  };
}

const turn = (step: string) => ({ step, role: step, round: 1, started_at: "", ended_at: null });

describe("progress", () => {
  it("marks the steps before the phase done", () => {
    expect(progress(record())).toEqual([
      { label: "spec", state: "done" }, { label: "build", state: "current" }, { label: "review", state: "todo" },
    ]);
  });

  it("puts quality after the step whose turn it judges", () => {
    expect(progress(record({ phase: "quality", turns: [turn("spec"), turn("build")] })).map((s) => [s.label, s.state]))
      .toEqual([["spec", "done"], ["build", "done"], ["quality", "current"], ["review", "todo"]]);
  });

  it("puts publish after every step", () => {
    expect(progress(record({ phase: "publish" })).map((s) => s.state)).toEqual(["done", "done", "done", "current"]);
  });

  it("marks every step done once the run is", () => {
    expect(progress(record({ phase: "done" })).map((s) => s.state)).toEqual(["done", "done", "done"]);
  });

  it("marks a failed run's stage failed", () => {
    expect(progress(record({ error: "boom" }))[1]).toEqual({ label: "build", state: "failed" });
  });

  it("shows only the phase of a workflow that could not be loaded", () => {
    expect(progress(record({ steps: null, phase: "impl" }))).toEqual([{ label: "impl", state: "current" }]);
  });
});

describe("wording", () => {
  it("formats ages as list does", () => {
    expect([0, 59, 60, 3599, 3600, 7380].map(formatAge)).toEqual(["0s", "59s", "1m", "59m", "1h0m", "2h3m"]);
  });

  it("formats the round with its quality round", () => {
    expect(formatRound(record())).toBe("round 1");
    expect(formatRound(record({ quality_round: 2 }))).toBe("round 1 q2");
  });

  it("shortens a GitHub pull request to its number", () => {
    expect(prLabel("https://github.com/o/r/pull/53")).toBe("#53");
    expect(prLabel("https://gitlab.example/o/r/-/merge_requests/7")).toBe("https://gitlab.example/o/r/-/merge_requests/7");
  });

  it("words the outcome", () => {
    expect(outcome(record(), 12)).toBe("running, beat 12s ago");
    expect(outcome(record({ health: "stale" }), 400)).toBe("stale, beat 6m ago");
    expect(outcome(record({ health: null, verdict: "APPROVE" }), 0)).toBe("APPROVE");
    expect(outcome(record({ error: "boom" }), 0)).toBe("error: boom");
  });
});

describe("applyEvent", () => {
  it("keeps a run's files across a new record, and ages from the record's arrival", () => {
    let state = applyEvent(NO_RUNS, { kind: "record", run: "r", record: record() }, 1000);
    state = applyEvent(state, { kind: "files", run: "r", files: [{ name: "spec.md", mtime: 1 }] }, 1000);
    state = applyEvent(state, { kind: "record", run: "r", record: record({ heartbeat_age: 0 }) }, 5000);
    const entry = state.runs.r;
    expect(entry?.files).toEqual([{ name: "spec.md", mtime: 1 }]);
    expect(entry && heartbeatAge(entry, 7900)).toBe(2);
  });

  it("ignores files of a run it does not know", () => {
    expect(applyEvent(NO_RUNS, { kind: "files", run: "gone", files: [] }, 0)).toBe(NO_RUNS);
  });
});
