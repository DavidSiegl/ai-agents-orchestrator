import { useEffect, useReducer, useState } from "react";
import { EVENT_KINDS, type Api, type Project, type RunEvent } from "./api";
import { applyEvent, NO_RUNS, type RunsState } from "./runs";

type Action = { type: "reset" } | { type: "event"; event: RunEvent; at: number };

function reduce(state: RunsState, action: Action): RunsState {
  return action.type === "reset" ? NO_RUNS : applyEvent(state, action.event, action.at);
}

/** The project's runs, kept up to date by the server's event stream for as long as the component is mounted.
 *
 * The stream starts with every run's record and files, also after the EventSource reconnects, so each opening
 * starts from no runs. A stream that cannot be opened shows as the state's problem until it reconnects.
 */
export function useRunStream(api: Api, project: Project): RunsState {
  const [state, dispatch] = useReducer(reduce, NO_RUNS);
  useEffect(() => {
    dispatch({ type: "reset" });
    const source = new EventSource(api.eventsUrl(project));
    source.addEventListener("open", () => dispatch({ type: "reset" }));
    source.addEventListener("error", () => {
      const event: RunEvent = { kind: "problem", error: "lost the connection to the orchestrator; retrying" };
      dispatch({ type: "event", event, at: Date.now() });
    });
    for (const kind of EVENT_KINDS) {
      source.addEventListener(kind, (message: MessageEvent<string>) => {
        const event = { kind, ...JSON.parse(message.data) } as RunEvent;
        dispatch({ type: "event", event, at: Date.now() });
      });
    }
    return () => source.close();
  }, [api, project]);
  return state;
}

/** The time, in milliseconds since the epoch, updated every intervalMs. */
export function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(timer);
  }, [intervalMs]);
  return now;
}
