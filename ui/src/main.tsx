import { invoke } from "@tauri-apps/api/core";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import type { Connection } from "./api";
import { App } from "./App";
import "./styles.css";

// The Tauri shell's `server` command: the sidecar's address and token, once `serve` printed them.
const connect = () => invoke<Connection>("server");

createRoot(document.getElementById("root") as HTMLElement).render(
  <StrictMode>
    <App connect={connect} />
  </StrictMode>,
);
