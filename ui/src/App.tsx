import { useEffect, useMemo, useState } from "react";
import { Api, type Connection, type Project } from "./api";
import { ProjectBar } from "./components/ProjectBar";
import { RunDetail } from "./components/RunDetail";
import { RunsOverview } from "./components/RunsOverview";
import { useNow, useRunStream } from "./hooks";
import { runsNewestFirst } from "./runs";

// The project last opened, kept per viewer across starts.
const PROJECT_KEY = "orchestrator.project";

function savedProject(fallback: string): Project {
  try {
    const saved = JSON.parse(localStorage.getItem(PROJECT_KEY) ?? "null") as Project | null;
    if (saved && typeof saved.cwd === "string" && typeof saved.machine === "string") {
      return saved;
    }
  } catch {
    // Storage may be unavailable or hold something else; the checkout's own project will do.
  }
  return { cwd: fallback, machine: "" };
}

function saveProject(project: Project): void {
  try {
    localStorage.setItem(PROJECT_KEY, JSON.stringify(project));
  } catch {
    // Remembering the project is a convenience.
  }
}

/** The app: the project bar, and the project's runs or one run in detail. connect resolves to the sidecar's
 * server, or rejects with why it could not be started. */
export function App({ connect }: { connect: () => Promise<Connection> }) {
  const [connection, setConnection] = useState<Connection | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  useEffect(() => {
    connect().then(setConnection, (error: unknown) => setFailure(String(error)));
  }, [connect]);
  if (failure) {
    return <p className="problem">Could not start the orchestrator: {failure}</p>;
  }
  return connection ? <Connected connection={connection} /> : <p className="loading">Starting the orchestrator…</p>;
}

function Connected({ connection }: { connection: Connection }) {
  const api = useMemo(() => new Api(connection), [connection]);
  const [project, setProject] = useState(() => savedProject(connection.project));
  const [selected, setSelected] = useState<string | null>(null);
  const state = useRunStream(api, project);
  const now = useNow();
  const open = (next: Project) => {
    saveProject(next);
    setSelected(null);
    setProject(next);
  };
  const entry = selected ? state.runs[selected] : undefined;
  return (
    <main>
      <ProjectBar project={project} onOpen={open} />
      {state.problem && <p className="problem">{state.problem}</p>}
      {entry ? (
        <RunDetail key={entry.record.id} api={api} project={project} entry={entry} now={now} onClose={() => setSelected(null)} />
      ) : (
        <RunsOverview runs={runsNewestFirst(state)} now={now} selected={selected} onSelect={setSelected} />
      )}
    </main>
  );
}
