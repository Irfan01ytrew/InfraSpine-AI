import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "@fontsource-variable/inter";
import { App } from "./App";
import { FeedbackProvider } from "./components/Feedback";
import "./index.css";
import { applyTheme, savedTheme } from "./theme";

// Apply the saved theme before the first render so the page never flashes the other palette.
applyTheme(savedTheme());

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <FeedbackProvider>
      <App />
    </FeedbackProvider>
  </StrictMode>,
);
