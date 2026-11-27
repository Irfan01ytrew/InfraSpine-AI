import { useEffect, useRef } from "react";

/** Fired to ask every live page to reload now (e.g. clicking the page you are already on). */
export const REFRESH_EVENT = "infraspine:refresh";
const MIN_GAP_MS = 10_000;

export function requestRefresh() {
  window.dispatchEvent(new Event(REFRESH_EVENT));
}

/**
 * Keep a page's data current without a manual Refresh: reloads every `intervalMs` while the tab is visible,
 * when the tab or window comes back into view, and on `requestRefresh()`. Calls never overlap, and a focus
 * reload is skipped if the page reloaded in the last few seconds. Pass a reload that does not reset what the
 * person is typing or show a full-page loading state.
 */
export function useLiveRefresh(reload: () => unknown, intervalMs = 60_000) {
  const latest = useRef(reload);
  latest.current = reload;

  useEffect(() => {
    let running = false;
    let last = Date.now();
    const run = async (force: boolean) => {
      if (running || document.visibilityState !== "visible") return;
      if (!force && Date.now() - last < MIN_GAP_MS) return;
      running = true;
      last = Date.now();
      try { await latest.current(); } catch { /* the page shows its own errors */ } finally { running = false; }
    };
    const onTick = () => void run(true);
    const onReturn = () => void run(false);
    const onRequest = () => void run(true);
    const timer = window.setInterval(onTick, intervalMs);
    document.addEventListener("visibilitychange", onReturn);
    window.addEventListener("focus", onReturn);
    window.addEventListener(REFRESH_EVENT, onRequest);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onReturn);
      window.removeEventListener("focus", onReturn);
      window.removeEventListener(REFRESH_EVENT, onRequest);
    };
  }, [intervalMs]);
}
