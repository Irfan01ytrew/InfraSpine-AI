import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "@fontsource-variable/inter";
import { App } from "./App";
import { FeedbackProvider } from "./components/Feedback";
import "./index.css";
import { applyTheme, savedTheme } from "./theme";

// Apply the saved theme before the first render so the page never flashes the other palette.
applyTheme(savedTheme());

// Step U: install the service worker in production only. The worker lives at
// /admin/sw.js with scope /admin/ (the admin UI is served from /admin by
// FastAPI); in dev the Vite server has no /admin/sw.js, so never register
// there. /api requests sit outside the worker's scope and always bypass it.
if (
  !import.meta.env.DEV &&
  typeof navigator !== "undefined" &&
  "serviceWorker" in navigator &&
  window.location.pathname.startsWith("/admin/")
) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/admin/sw.js", { scope: "/admin/" }).catch(() => {
      // Offline support is best-effort; the app works without it.
    });
  });
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <FeedbackProvider>
      <App />
    </FeedbackProvider>
  </StrictMode>,
);
