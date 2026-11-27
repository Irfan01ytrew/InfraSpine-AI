import { useState } from "react";
import { formatDate, timeAgo } from "../format";
import type { AuditEntry } from "../types";

const ACTION_LABELS: Record<string, string> = {
  "auth.login": "Sign-in",
  "auth.logout": "Sign-out",
  "auth.password_reset": "Password reset",
  "provider.config_created": "Provider configured",
  "provider.custom_created": "Custom provider added",
  "provider.config_updated": "Configuration updated",
  "provider.config_update": "Configuration update",
  "provider.connection_tested": "Connection test",
  "provider.credential_staged": "API key verified",
  "provider.credential_stage": "API key verification",
  "provider.activate": "Activation",
  "provider.activated": "Provider activated",
  "provider.deactivated": "Provider deactivated",
  "provider.removed": "Provider removed",
  "provider.endpoint_changed": "API URL changed",
  "provider.endpoint_change": "API URL verification",
  "provider.endpoint_rejected": "API URL rejected",
  "provider.credential_rebound": "Key bound to API URL",
  "document.processing_cancelled": "Document processing cancelled",
};

const REASON_LABELS: Record<string, string> = {
  invalid_credentials: "key rejected",
  model_unavailable: "model unavailable",
  timeout: "timed out",
  rate_limited: "rate limited",
  provider_unavailable: "provider unreachable",
  unsafe_endpoint: "unsafe API URL",
  redirect_refused: "redirect refused",
  bad_password: "wrong password",
  bad_password_locked: "wrong password, account locked",
  unknown_user: "unknown email",
  locked: "account locked",
  inactive_user: "inactive account",
};

function detailChips(entry: AuditEntry): string[] {
  const d = entry.details;
  const chips: string[] = [];
  if (typeof d.reason === "string") chips.push(REASON_LABELS[d.reason] ?? d.reason);
  if (typeof d.model === "string") chips.push(d.model);
  if (typeof d.origin === "string") chips.push(d.origin);
  if (typeof d.key_hint === "string") chips.push(`key ••••${d.key_hint}`);
  if (d.rotated_from_credential_id) chips.push("rotation");
  if (typeof d.tested === "string") chips.push(d.tested.replace("_", " "));
  return chips;
}

type OutcomeFilter = "all" | "success" | "failure";

export function AuditLog({ entries }: { entries: AuditEntry[] }) {
  const [outcome, setOutcome] = useState<OutcomeFilter>("all");
  const [query, setQuery] = useState("");

  const needle = query.trim().toLowerCase();
  const visible = entries.filter(
    (e) =>
      (outcome === "all" || e.outcome === outcome) &&
      (!needle ||
        [ACTION_LABELS[e.action] ?? e.action, e.action, e.actor_email ?? "", e.target ?? "", ...detailChips(e)]
          .join(" ")
          .toLowerCase()
          .includes(needle)),
  );
  const failures = entries.filter((e) => e.outcome === "failure").length;

  return (
    <section className="card card-flush">
      <div className="log-toolbar">
        <div className="segmented" role="radiogroup" aria-label="Filter by result">
          {(
            [
              ["all", `All ${entries.length}`],
              ["success", "Succeeded"],
              ["failure", `Failed ${failures}`],
            ] as [OutcomeFilter, string][]
          ).map(([value, label]) => (
            <button
              key={value}
              type="button"
              role="radio"
              aria-checked={outcome === value}
              className={outcome === value ? "segment segment-active" : "segment"}
              onClick={() => setOutcome(value)}
            >
              {label}
            </button>
          ))}
        </div>
        <input
          className="log-search"
          type="search"
          placeholder="Search user, action, model…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          aria-label="Search logs"
        />
      </div>
      {visible.length === 0 ? (
        <p className="empty">{entries.length === 0 ? "No activity yet." : "No events match these filters."}</p>
      ) : (
        <ul className="timeline">
          {visible.map((e) => (
            <li key={e.id} className="timeline-item">
              <span className={`dot ${e.outcome === "success" ? "dot-success" : "dot-error"}`} />
              <div className="timeline-body">
                <div className="timeline-main">
                  <strong>{ACTION_LABELS[e.action] ?? e.action}</strong>
                  {e.outcome === "failure" && <span className="pill pill-error pill-sm">failed</span>}
                  {e.target && <span className="muted">· {e.target}</span>}
                </div>
                <div className="timeline-meta">
                  <span>{e.actor_email ?? "system"}</span>
                  {detailChips(e).map((chip) => (
                    <span key={chip} className="chip chip-sm">
                      {chip}
                    </span>
                  ))}
                </div>
              </div>
              <time className="timeline-time" dateTime={e.created_at} title={formatDate(e.created_at)}>
                {timeAgo(e.created_at)}
              </time>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
