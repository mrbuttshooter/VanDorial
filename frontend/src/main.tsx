import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./styles/global.css";

const root = document.getElementById("root");
if (!root) throw new Error("#root element not found");

// App owns the auth gate: it verifies any stored token (falling back to the
// legacy console bootstrap once) and shows the Login page if nobody is signed
// in. Rendering happens immediately; the gate resolves inside React.
createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
