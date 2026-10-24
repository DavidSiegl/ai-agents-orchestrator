import type { RunRecord } from "../api";
import { progress } from "../runs";

/** The run's stages in order, each marked done, current, to do or failed. */
export function StepProgress({ record }: { record: RunRecord }) {
  return (
    <ol className="progress" aria-label="progress">
      {progress(record).map((stage) => (
        <li key={stage.label} className={`stage ${stage.state}`} data-state={stage.state}>
          {stage.label}
        </li>
      ))}
    </ol>
  );
}
