import type { RunEvent, RunFile, RunRecord } from "./api";

/** A run as the app last heard of it. */
export interface RunEntry {
  record: RunRecord;
  /** When the record arrived, in milliseconds since the epoch: its heartbeat_age was the age then. */
  receivedAt: number;
  /** null until the run's first files event. */
  files: RunFile[] | null;
}

/** What a project's event stream has told the app. */
export interface RunsState {
  runs: Record<string, RunEntry>;
  /** Why the server could not read the project, as its last problem event said; null once it can. */
  problem: string | null;
}

export const NO_RUNS: RunsState = { runs: {}, problem: null };

/** The state after an event that arrived at the time `at`, in milliseconds since the epoch. */
export function applyEvent(state: RunsState, event: RunEvent, at: number): RunsState {
  switch (event.kind) {
    case "problem":
      return { ...state, problem: event.error };
    case "record": {
      const { [event.run]: old, ...others } = state.runs;
      if (event.record === null) {
        return { ...state, runs: others };
      }
      const entry = { record: event.record, receivedAt: at, files: old?.files ?? null };
      return { ...state, runs: { ...others, [event.run]: entry } };
    }
    case "files": {
      const old = state.runs[event.run];
      // The server sends a run's record before its files, so a run unknown here has gone since.
      return old ? { ...state, runs: { ...state.runs, [event.run]: { ...old, files: event.files } } } : state;
    }
  }
}

/** The runs, newest first: run ids start with their start time. */
export function runsNewestFirst(state: RunsState): RunEntry[] {
  return Object.values(state.runs).sort((a, b) => b.record.id.localeCompare(a.record.id));
}

/** The seconds since the run's state.json was written, at the time now in milliseconds since the epoch. */
export function heartbeatAge(entry: RunEntry, now: number): number {
  return entry.record.heartbeat_age + Math.max(0, Math.floor((now - entry.receivedAt) / 1000));
}

/** seconds as `list` words an age (orchestrator.py's format_age): 42s, 5m, 2h3m. */
export function formatAge(seconds: number): string {
  if (seconds < 60) {
    return `${seconds}s`;
  }
  if (seconds < 3600) {
    return `${Math.floor(seconds / 60)}m`;
  }
  return `${Math.floor(seconds / 3600)}h${Math.floor((seconds % 3600) / 60)}m`;
}

/** A pull request's URL as the overview shows it: #N for a GitHub pull request, else the whole URL. */
export function prLabel(url: string): string {
  const number = url.match(/\/pull\/(\d+)$/)?.[1];
  return number ? `#${number}` : url;
}

/** The round as `list` words it (run_round): "round 2", with " q1" in a quality round. */
export function formatRound(record: RunRecord): string {
  return record.quality_round ? `round ${record.round} q${record.quality_round}` : `round ${record.round}`;
}

export type StageState = "done" | "current" | "todo" | "failed";

/** One stage of a run's progress: a step of its workflow, or one of the reserved phases quality and publish. */
export interface Stage {
  label: string;
  state: StageState;
}

/** The run's way through its workflow, by its steps and its phase.
 *
 * The current stage is the phase's step; in quality, a stage of its own after the step whose turn it judges, the
 * last one taken; in publish, a stage after every step. A failed run's current stage is "failed". A workflow this
 * orchestrator could not load has no steps, and its progress is its phase alone.
 */
export function progress(record: RunRecord): Stage[] {
  const current: StageState = record.error ? "failed" : "current";
  if (!record.steps) {
    return [{ label: record.phase, state: current }];
  }
  const ids = record.steps.map((step) => step.id);
  const stages = (doneBefore: number): Stage[] =>
    ids.map((id, i) => ({ label: id, state: i < doneBefore ? "done" : "todo" }));
  if (record.phase === "done") {
    return stages(ids.length);
  }
  if (record.phase === "publish") {
    return [...stages(ids.length), { label: "publish", state: current }];
  }
  if (record.phase === "quality") {
    const lastTurn = [...record.turns].reverse().find((turn) => ids.includes(turn.step));
    const judged = lastTurn ? ids.indexOf(lastTurn.step) : ids.length - 1;
    const all = stages(judged + 1);
    return [...all.slice(0, judged + 1), { label: "quality", state: current }, ...all.slice(judged + 1)];
  }
  const at = ids.indexOf(record.phase);
  return stages(at).map((stage, i) => (i === at ? { ...stage, state: current } : stage));
}

/** How the run is doing, as the overview words it: its error, verdict, block, or health with a ticking beat. */
export function outcome(record: RunRecord, age: number): string {
  if (record.error) {
    return `error: ${record.error}`;
  }
  if (record.health === null) {
    return record.verdict ?? "";
  }
  return `${record.health}, beat ${formatAge(age)} ago`;
}
