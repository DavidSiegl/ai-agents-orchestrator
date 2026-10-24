import { useState, type FormEvent } from "react";
import type { Project } from "../api";

/** The project folder and machine fields; onOpen gets the project they name when the form is submitted. */
export function ProjectBar({ project, onOpen }: { project: Project; onOpen: (project: Project) => void }) {
  const [cwd, setCwd] = useState(project.cwd);
  const [machine, setMachine] = useState(project.machine);
  const submit = (event: FormEvent) => {
    event.preventDefault();
    onOpen({ cwd: cwd.trim(), machine: machine.trim() });
  };
  return (
    <form className="project-bar" onSubmit={submit}>
      <label>
        Project folder
        <input value={cwd} onChange={(e) => setCwd(e.target.value)} placeholder="~/GitHub/myproject" />
      </label>
      <label>
        Machine
        <input value={machine} onChange={(e) => setMachine(e.target.value)} placeholder="this one" />
      </label>
      <button type="submit" disabled={!cwd.trim()}>
        Open
      </button>
    </form>
  );
}
