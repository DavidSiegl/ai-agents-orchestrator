// The protocol of `orchestrator.py serve`: its run records, file lists and events. The server's RunRequestHandler
// and RunWatch document the endpoints and the events, and run_record every key of a record.

/** Where the sidecar's server listens, as the Tauri shell got it from `serve`'s first line. */
export interface Connection {
  url: string;
  token: string;
  /** The project to show first: the checkout the sidecar's orchestrator.py is in; null for a release's bundled
   * orchestrator, which is in none. */
  project: string | null;
}

/** A project as the server takes it: a directory, on the saved herdr machine or, with machine empty, here. */
export interface Project {
  cwd: string;
  machine: string;
}

export interface Step {
  id: string;
  role: string;
}

export interface Turn {
  step: string;
  role: string;
  round: number;
  started_at: string;
  ended_at: string | null;
}

/** One run as `list --json` prints it (orchestrator.py's run_record); null means the key does not apply. */
export interface RunRecord {
  id: string;
  key: string;
  task: string;
  workflow: string;
  steps: Step[] | null;
  phase: string;
  round: number;
  quality_round: number | null;
  role: string | null;
  pane: string | null;
  health: "running" | "stale" | null;
  health_reason: string | null;
  /** Seconds since state.json was written, when the server read it. */
  heartbeat_age: number;
  waiting: { reason: "blocked" | "interview" | "stale"; detail: string } | null;
  verdict: string | null;
  error: string | null;
  closed: string | null;
  branch: string | null;
  worktree: string | null;
  pr: { url: string; state: string | null; error: string | null } | null;
  turns: Turn[];
  resume_command: string | null;
}

/** A file in a run directory; mtime is in seconds since the epoch. */
export interface RunFile {
  name: string;
  mtime: number;
}

/** An event of the server's /events stream, its SSE event name as kind. */
export type RunEvent =
  | { kind: "record"; run: string; record: RunRecord | null }
  | { kind: "files"; run: string; files: RunFile[] }
  | { kind: "problem"; error: string | null };

export const EVENT_KINDS = ["record", "files", "problem"] as const;

/** The server's endpoints for one connection. */
export class Api {
  constructor(private readonly connection: Connection) {}

  /** The URL of the project's event stream, the token in it, since an EventSource cannot set a header. */
  eventsUrl(project: Project): string {
    return this.url("/events", project, { token: this.connection.token });
  }

  /** The text of a run's file; rejects with the server's error message for a missing file or a failed read. */
  async fileText(project: Project, run: string, name: string): Promise<string> {
    const response = await fetch(this.url("/file", project, { run, name }), {
      headers: { Authorization: `Bearer ${this.connection.token}` },
    });
    const body = (await response.json()) as { text?: string; error?: string };
    if (!response.ok || body.text === undefined) {
      throw new Error(body.error ?? `HTTP ${response.status}`);
    }
    return body.text;
  }

  private url(path: string, project: Project, params: Record<string, string>): string {
    const query = new URLSearchParams({ cwd: project.cwd, ...params });
    if (project.machine) {
      query.set("machine", project.machine);
    }
    return `${this.connection.url}${path}?${query}`;
  }
}
