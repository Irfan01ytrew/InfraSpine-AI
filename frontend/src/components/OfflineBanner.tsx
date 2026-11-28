// Offline banner + queued-item indicator (Step U, slice 2).
//
// Shows three things, only when they matter:
// - An offline banner while the browser is offline, so the user knows new
//   actions will be queued instead of sent.
// - A "Queued (n)" indicator with the per-item status of every pending or
//   finished offline action: queued / syncing / synced / dropped, with the
//   drop reason next to dropped items (expired, changed on the server,
//   rejected, or 3 failed tries).
// - Dropped-item notices: dropped items stay visible until the user
//   dismisses them, so lost work is never silent.
//
// The banner also starts the offline sync wiring (offlineSync.ts): expired
// entries are dropped on start, and the queue replays when the browser comes
// back online. Nothing here sends, stores or replays credentials — replay
// uses the browser's same-origin session cookie, exactly like a normal
// request.
import { useCallback, useEffect, useState } from "react";
import { IconAlert, IconCheck, IconInfo, IconX } from "../icons";
import { cn } from "../lib/utils";
import {
  countByStatus,
  deriveItems,
  dropReasonLabel,
  kindLabel,
  statusLabel,
} from "../lib/offlineStatus.mjs";
import type { OfflineStatusItem } from "../lib/offlineStatus.mjs";
import {
  OFFLINE_QUEUE_CHANGED_EVENT,
  dismissAllFinished,
  dismissOfflineItem,
  readOfflineState,
  retryOfflineSync,
  startOfflineSync,
} from "../lib/offlineSync";
import type { OfflineSnapshot } from "../lib/offlineSync";

function useOfflineQueue(): OfflineSnapshot {
  const [snapshot, setSnapshot] = useState<OfflineSnapshot>(() => readOfflineState());
  const refresh = useCallback(() => setSnapshot(readOfflineState()), []);
  useEffect(() => {
    const stop = startOfflineSync();
    window.addEventListener("online", refresh);
    window.addEventListener("offline", refresh);
    window.addEventListener(OFFLINE_QUEUE_CHANGED_EVENT, refresh);
    window.addEventListener("storage", refresh);
    return () => {
      window.removeEventListener("online", refresh);
      window.removeEventListener("offline", refresh);
      window.removeEventListener(OFFLINE_QUEUE_CHANGED_EVENT, refresh);
      window.removeEventListener("storage", refresh);
      stop();
    };
  }, [refresh]);
  return snapshot;
}

function statusDot(status: OfflineStatusItem["status"]): string {
  switch (status) {
    case "queued":
      return "dot-warning";
    case "syncing":
      return "dot-warning dot-pulse";
    case "synced":
      return "dot-success";
    case "dropped":
      return "dot-error";
    default:
      return "dot-muted";
  }
}

function formatQueuedAt(createdAt: number): string {
  return new Date(createdAt).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function OfflineBanner() {
  const snapshot = useOfflineQueue();
  const [expanded, setExpanded] = useState(false);
  const items = deriveItems(snapshot.queue, snapshot.records, snapshot.syncingIds);
  const counts = countByStatus(items);
  const finishedCount = counts.synced + counts.dropped;

  // Nothing to say: online, nothing pending, nothing finished to report.
  if (snapshot.online && items.length === 0) return null;

  return (
    <div className="offline-banner shrink-0 px-4 pt-4 sm:px-6 lg:px-8" role="status" aria-live="polite">
      {!snapshot.online && (
        <div className="banner banner-warning">
          <IconAlert size={16} />
          <div>
            <strong>You&rsquo;re offline.</strong>{" "}
            {counts.queued + counts.syncing > 0
              ? `Actions you take now will be queued and synced when you reconnect — ${counts.queued + counts.syncing} waiting.`
              : "Actions you take now will be queued and synced when you reconnect."}
          </div>
        </div>
      )}
      {snapshot.online && counts.syncing > 0 && (
        <div className="banner banner-info">
          <IconInfo size={16} />
          <div>
            <strong>Back online.</strong> Syncing {counts.syncing} queued action
            {counts.syncing === 1 ? "" : "s"}&hellip;
          </div>
        </div>
      )}
      {items.length > 0 && (
        <div className="offline-queue-panel">
          <button
            type="button"
            className="offline-queue-toggle"
            aria-expanded={expanded}
            onClick={() => setExpanded((current) => !current)}
          >
            <span>
              Queued ({counts.queued + counts.syncing})
              {counts.dropped > 0 ? ` · Dropped (${counts.dropped})` : ""}
              {counts.synced > 0 ? ` · Synced (${counts.synced})` : ""}
            </span>
            <span aria-hidden="true">{expanded ? "▴" : "▾"}</span>
          </button>
          {expanded && (
            <>
              <ul className="offline-queue-list">
                {items.map((item) => (
                  <li key={item.id} className="offline-queue-item">
                    <span className={cn("dot", statusDot(item.status))} aria-hidden="true" />
                    <span className="offline-queue-item-text">
                      <span>
                        {kindLabel(item.kind)} — {statusLabel(item.status)}
                      </span>
                      <small>{formatQueuedAt(item.createdAt)}</small>
                      {item.status === "dropped" && item.reason && (
                        <small className="offline-queue-item-reason">{dropReasonLabel(item.reason)}</small>
                      )}
                    </span>
                    {(item.status === "synced" || item.status === "dropped") && (
                      <button
                        type="button"
                        className="icon-btn"
                        aria-label={`Dismiss ${kindLabel(item.kind)}`}
                        onClick={() => dismissOfflineItem(item.id)}
                      >
                        <IconX size={14} />
                      </button>
                    )}
                  </li>
                ))}
              </ul>
              <div className="offline-queue-actions">
                {snapshot.online && counts.queued > 0 && (
                  <button type="button" className="btn btn-sm" onClick={() => retryOfflineSync()}>
                    Retry now
                  </button>
                )}
                {finishedCount > 0 && (
                  <button type="button" className="btn btn-sm btn-ghost" onClick={() => dismissAllFinished()}>
                    Dismiss all ({finishedCount})
                  </button>
                )}
              </div>
            </>
          )}
        </div>
      )}
      {snapshot.online && counts.syncing === 0 && counts.queued === 0 && counts.synced > 0 && counts.dropped === 0 && (
        <div className="banner banner-info">
          <IconCheck size={16} />
          <div>All queued actions synced.</div>
        </div>
      )}
    </div>
  );
}
