import { type FormEvent, useEffect, useRef, useState } from "react";
import type { AgentInput, AgentRef, Api, Project, RunRecord } from "../api";

/** How often the shown agent's terminal is read again, in milliseconds. */
export const AGENT_POLL_MS = 2000;

/** The lines of an agent's terminal shown. */
export const AGENT_LINES = 200;

/** The keys the section offers as buttons, as herdr names them, with their labels. */
const KEYS: [string, string][] = [
  ["esc", "Esc"],
  ["enter", "Enter"],
  ["up", "↑"],
  ["down", "↓"],
  ["ctrl+c", "Ctrl+C"],
];

interface Props {
  api: Api;
  project: Project;
  record: RunRecord;
}

/** The run's agents, a tab each: what the chosen one's pane shows, read again every AGENT_POLL_MS while it is shown,
 * and a box and keys to type into it, as into its herdr pane. The current role's agent is chosen until another is. */
export function AgentPanes({ api, project, record }: Props) {
  const agents = record.agents ?? [];
  const [chosen, setChosen] = useState<string | null>(null);
  const shown = agents.find((a) => a.role === chosen) ?? agents.find((a) => a.role === record.role) ?? agents[0];
  if (!shown) {
    return (
      <section className="agents" aria-label="agents">
        <p className="empty">No agent has started yet.</p>
      </section>
    );
  }
  return (
    <section className="agents" aria-label="agents">
      <div role="tablist" aria-label="agents">
        {agents.map((a) => (
          <button
            key={a.role}
            type="button"
            role="tab"
            aria-selected={a.role === shown.role}
            title={`${a.name} (${a.kind})`}
            onClick={() => setChosen(a.role)}
          >
            {a.role}
          </button>
        ))}
      </div>
      {/* Keyed by the agent, so another one starts from nothing read and nothing typed. */}
      <AgentPane key={shown.name} api={api} project={project} run={record.id} agent={shown} />
    </section>
  );
}

/** What one agent's pane shows, and the input to it. */
function AgentPane({ api, project, run, agent }: { api: Api; project: Project; run: string; agent: AgentRef }) {
  const { text, problem, refresh } = useAgentText(api, project, run, agent.role);
  const [draft, setDraft] = useState("");
  const [sendProblem, setSendProblem] = useState<string | null>(null);
  const screen = useRef<HTMLPreElement>(null);
  useEffect(() => {
    // Follow the end of the output, as the pane does.
    screen.current?.scrollTo?.({ top: screen.current.scrollHeight });
  }, [text]);

  const type = (input: AgentInput) =>
    api.typeInto(project, run, agent.role, input).then(
      () => {
        setSendProblem(null);
        refresh();
        return true;
      },
      (error: Error) => {
        setSendProblem(error.message);
        return false;
      },
    );
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (draft.trim()) {
      void type({ text: draft }).then((sent) => sent && setDraft(""));
    }
  };

  return (
    <div className="agent" role="tabpanel" aria-label={`${agent.role} agent`}>
      {problem && <p className="problem">{problem}</p>}
      <pre ref={screen} aria-label={`what the ${agent.role} agent shows`}>
        {text ?? "Reading…"}
      </pre>
      <form onSubmit={submit}>
        <input
          aria-label={`Message to the ${agent.role} agent`}
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          placeholder="Type to the agent, as in its pane"
        />
        <button type="submit" disabled={!draft.trim()}>
          Send
        </button>
        {KEYS.map(([key, label]) => (
          <button key={key} type="button" aria-label={`press ${key}`} onClick={() => void type({ keys: [key] })}>
            {label}
          </button>
        ))}
      </form>
      {sendProblem && <p className="problem">{sendProblem}</p>}
    </div>
  );
}

/** The last AGENT_LINES lines of what the agent shows, read now and every AGENT_POLL_MS while mounted, one read at a
 * time; text keeps the last read that succeeded, and problem says why the last one failed, null once one succeeds.
 * refresh reads again at once. */
function useAgentText(api: Api, project: Project, run: string, role: string) {
  const [text, setText] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [reads, setReads] = useState(0);
  useEffect(() => {
    let current = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const read = () => {
      api.agentText(project, run, role, AGENT_LINES).then(
        (next) => {
          if (current) {
            setText(next);
            setProblem(null);
          }
        },
        (error: Error) => current && setProblem(error.message),
      ).finally(() => {
        if (current) {
          timer = setTimeout(read, AGENT_POLL_MS);
        }
      });
    };
    read();
    return () => {
      current = false;
      clearTimeout(timer);
    };
  }, [api, project, run, role, reads]);
  return { text, problem, refresh: () => setReads((n) => n + 1) };
}
