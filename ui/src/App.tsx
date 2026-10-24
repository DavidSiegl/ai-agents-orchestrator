import { useEffect, useMemo, useState } from "react";
import { Api, type Connection, type Project } from "./api";
import { ProjectBar } from "./components/ProjectBar";
import { RunDetail } from "./components/RunDetail";
import { RunsOverview } from "./components/RunsOverview";
import { useNow, useRunStream } from "./hooks";
import { runsNewestFirst } from "./runs";

// The project last opened, kept per viewer across starts.
const PROJECT_KEY = "orchestrator.project";

/** The project last opened, else fallback's; a project with an empty cwd when there is neither. */
function savedProject(fallback: string | null): Project {
  try {
    const saved = JSON.parse(localStorage.getItem(PROJECT_KEY) ?? "null") as Project | null;
    if (saved && typeof saved.cwd === "string" && typeof saved.machine === "string") {
      return saved;
    }
  } catch {
    // Storage may be unavailable or hold something else; the fallback will do.
  }
  return { cwd: fallback ?? "", machine: "" };
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
  const open = (next: Project) => {
    saveProject(next);
    setProject(next);
  };
  return (
    <main>
      <ProjectBar project={project} onOpen={open} />
      {project.cwd ? (
        // Keyed by the project, so opening another one starts from its overview.
        <ProjectRuns key={`${project.machine}:${project.cwd}`} api={api} project={project} />
      ) : (
        <p className="empty">Open a project folder to see its runs.</p>
      )}
    </main>
  );
}

/** One project's runs, live: the overview, or the run picked from it in detail. */
function ProjectRuns({ api, project }: { api: Api; project: Project }) {
  const [selected, setSelected] = useState<string | null>(null);
  const state = useRunStream(api, project);
  const now = useNow();
  const entry = selected ? state.runs[selected] : undefined;
  return (
    <>
      {state.problem && <p className="problem">{state.problem}</p>}
      {entry ? (
        <RunDetail key={entry.record.id} api={api} project={project} entry={entry} now={now} onClose={() => setSelected(null)} />
      ) : (
        <RunsOverview runs={runsNewestFirst(state)} now={now} selected={selected} onSelect={setSelected} />
      )}
    </>
  );
}
