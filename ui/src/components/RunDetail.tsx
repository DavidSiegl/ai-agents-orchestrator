import { useState } from "react";
import type { Api, Project } from "../api";
import type { RunEntry } from "../runs";
import { formatRound, heartbeatAge, outcome } from "../runs";
import { AgentPanes } from "./AgentPanes";
import { HandoffFile } from "./HandoffFile";
import { StepProgress } from "./StepProgress";

interface Props {
  api: Api;
  project: Project;
  entry: RunEntry;
  /** The time, in milliseconds since the epoch, that the heartbeat is aged to. */
  now: number;
  onClose: () => void;
}

/** One run: its progress, its files, and the selected one rendered, the newest file until one is chosen; and its
 * agents. */
export function RunDetail({ api, project, entry, now, onClose }: Props) {
  const { record, files } = entry;
  const [chosen, setChosen] = useState<string | null>(null);
  const file = files?.find((f) => f.name === chosen) ?? files?.at(-1);
  return (
    <section className="detail" aria-label={`run ${record.key}`}>
      <header>
        <button type="button" onClick={onClose}>
          ‹ Runs
        </button>
        <h2 title={record.id}>{record.key}</h2>
        <p className="task">{record.task}</p>
        <p className="status">
          {formatRound(record)}
          {record.role && ` · ${record.role}`} · {outcome(record, heartbeatAge(entry, now))}
        </p>
        {record.waiting?.reason === "blocked" && <p className="blocked">blocked: {record.waiting.detail}</p>}
        {record.pr && (
          <a href={record.pr.url} target="_blank" rel="noreferrer">
            {record.pr.url}
          </a>
        )}
      </header>
      <StepProgress record={record} />
      <div className="files-and-file">
        <ul className="files" aria-label="files">
          {(files ?? []).map((f) => (
            <li key={f.name}>
              <button
                type="button"
                className={f.name === file?.name ? "selected" : undefined}
                onClick={() => setChosen(f.name)}
              >
                {f.name}
                <time dateTime={new Date(f.mtime * 1000).toISOString()}>
                  {new Date(f.mtime * 1000).toLocaleTimeString()}
                </time>
              </button>
            </li>
          ))}
        </ul>
        {file ? (
          <HandoffFile api={api} project={project} run={record.id} file={file} />
        ) : (
          <p className="empty">{files ? "No files yet." : "Loading…"}</p>
        )}
      </div>
      <AgentPanes api={api} project={project} record={record} />
    </section>
  );
}
