import { useEffect, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Api, Project, RunFile } from "../api";

interface Props {
  api: Api;
  project: Project;
  run: string;
  file: RunFile;
}

type Loaded = { text: string } | { error: string } | null;

/** The file rendered as Markdown, raw HTML in it dropped; fetched again whenever its mtime changes. */
export function HandoffFile({ api, project, run, file }: Props) {
  // What was fetched, by the request it answers, so a file selected since shows as loading, not as the last one.
  const [loaded, setLoaded] = useState<{ request: string; content: Loaded }>({ request: "", content: null });
  const request = JSON.stringify([project, run, file.name, file.mtime]);
  useEffect(() => {
    let current = true;
    api.fileText(project, run, file.name).then(
      (text) => current && setLoaded({ request, content: { text } }),
      (error: Error) => current && setLoaded({ request, content: { error: error.message } }),
    );
    return () => {
      current = false;
    };
  }, [api, project, run, file.name, request]);
  const content = loaded.request === request ? loaded.content : null;
  if (content === null) {
    return <p className="loading">Loading {file.name}…</p>;
  }
  if ("error" in content) {
    return <p className="problem">{content.error}</p>;
  }
  return (
    <article className="markdown">
      <Markdown remarkPlugins={[remarkGfm]} skipHtml>
        {content.text}
      </Markdown>
    </article>
  );
}
