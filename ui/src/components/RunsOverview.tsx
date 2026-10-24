import type { RunEntry } from "../runs";
import { formatRound, heartbeatAge, outcome, prLabel } from "../runs";
import { StepProgress } from "./StepProgress";

interface Props {
  runs: RunEntry[];
  /** The time, in milliseconds since the epoch, that the heartbeats are aged to. */
  now: number;
  selected: string | null;
  onSelect: (run: string) => void;
}

/** A table of the project's runs, one row per run; a click on a row selects its run. */
export function RunsOverview({ runs, now, selected, onSelect }: Props) {
  if (runs.length === 0) {
    return <p className="empty">No runs.</p>;
  }
  return (
    <table className="runs">
      <thead>
        <tr>
          <th>Run</th>
          <th>Task</th>
          <th>Progress</th>
          <th>Round</th>
          <th>Role</th>
          <th>Status</th>
          <th>Pull request</th>
        </tr>
      </thead>
      <tbody>
        {runs.map((entry) => {
          const { record } = entry;
          return (
            <tr
              key={record.id}
              data-run={record.id}
              className={record.id === selected ? "selected" : undefined}
              onClick={() => onSelect(record.id)}
            >
              <td className="key" title={record.id}>
                {record.key}
              </td>
              <td className="task" title={record.task}>
                {record.task}
              </td>
              <td>
                <StepProgress record={record} />
              </td>
              <td>{formatRound(record)}</td>
              <td className="role">{record.role ?? ""}</td>
              <td className={`status ${record.error ? "failed" : (record.health ?? "finished")}`}>
                {outcome(record, heartbeatAge(entry, now))}
                {record.waiting?.reason === "blocked" && <div className="blocked">blocked: {record.waiting.detail}</div>}
              </td>
              <td>
                {record.pr && (
                  <a href={record.pr.url} title={record.pr.url} target="_blank" rel="noreferrer">
                    {prLabel(record.pr.url)}
                  </a>
                )}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
