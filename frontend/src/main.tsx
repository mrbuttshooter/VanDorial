import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { bootstrapApiKey } from "./lib/api";
import "./styles/global.css";

const root = document.getElementById("root");
if (!root) throw new Error("#root element not found");

// Fetch the console's API key from the controller BEFORE the first render, so
// the initial data fetches (nodes, loops, stats) carry it and any browser that
// opens /console sees the data without pasting a key. Renders regardless if the
// bootstrap endpoint is absent (404) or the backend is unreachable.
bootstrapApiKey().finally(() => {
  createRoot(root).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
});
