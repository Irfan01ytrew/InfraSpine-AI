import { useCallback, useEffect, useState } from "react";
import { api, describeError } from "../api";
import { formatDate, timeAgo } from "../format";
import { Spinner } from "../icons";
import { useLiveRefresh } from "../lib/useLiveRefresh";
import type { DatabaseSummary } from "../types";
import { Alert, Button, PageHeader, Stat } from "./ui";

function bandLabel(band: string): string {
  return band === "ok" ? "OK" : band === "warning" ? "Warning" : band === "critical" ? "Critical" : "Unknown";
}

function bytesLabel(bytes: number): string {
  if (bytes >= 1_073_741_824) return `${(bytes / 1_073_741_824).toFixed(1)} GB`;
  if (bytes >= 1_048_576) return `${(bytes / 1_048_576).toFixed(1)} MB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${bytes} B`;
}

export function DatabaseOpsPage() {
  const [summary, setSummary] = useState<DatabaseSummary | null>(null);
  const [error, setError] = useState("");
  const load = useCallback(() => {
    setError("");
    return api.databaseSummary().then(setSummary, (err: unknown) => setError(describeError(err)));
  }, []);
  useEffect(() => {
    void load();
  }, [load]);
  useLiveRefresh(load);

  return (
    <>
      <PageHeader
        title="Database operations"
        actions={<><Button onClick={() => void load()}>Refresh</Button></>}
      />
      {error ? (
        <Alert tone="danger" className="mb-4">{error}</Alert>
      ) : !summary ? (
        <div className="loading">
          <Spinner size={22} />
        </div>
      ) : (
        <>
          <div className="stats">
            <Stat label="Health" value={bandLabel(summary.health.overall)} hint={summary.health.reachable ? "reachable" : "unreachable"} />
            <Stat label="Size" value={bytesLabel(summary.health.database_bytes)} hint={summary.database} />
            <Stat
              label="Connections"
              value={`${summary.health.connections.total} / ${summary.health.connections.max}`}
              hint={`${summary.health.connections.active} active · ${summary.health.connections.idle_in_transaction} idle in transaction`}
            />
            <Stat
              label="Integrity"
              value={summary.integrity.passed ? "Passed" : "Failed"}
              hint={`${summary.integrity.foreign_keys} foreign keys · ${summary.integrity.total_orphans} orphans`}
            />
            <Stat
              label="Migration gate"
              value={summary.migration_gate.allowed ? "Allowed" : "Blocked"}
              hint={`RPO ${summary.migration_gate.rpo_hours}h`}
            />
          </div>

          <div className="card mb-4 p-4">
            <h2 className="font-medium">Latest backup</h2>
            {summary.newest_backup ? (
              <dl className="mt-3 grid gap-2 text-sm sm:grid-cols-2">
                <div><dt className="text-muted">Created</dt><dd>{summary.newest_backup.created_at ? `${formatDate(summary.newest_backup.created_at)} (${timeAgo(summary.newest_backup.created_at)})` : "—"}</dd></div>
                <div><dt className="text-muted">Status</dt><dd>{summary.newest_backup.status}{summary.newest_backup.drill_verified ? " · drill passed" : " · drill not passed"}</dd></div>
                <div><dt className="text-muted">Size</dt><dd>{bytesLabel(summary.newest_backup.bytes)}</dd></div>
                <div><dt className="text-muted">Catalog</dt><dd>{summary.newest_backup.catalog_entries} entries · {summary.newest_backup.tables ?? "?"} tables · {(summary.newest_backup.rows_total ?? 0).toLocaleString()} rows</dd></div>
                <div className="sm:col-span-2"><dt className="text-muted">SHA-256</dt><dd className="break-all font-mono text-xs">{summary.newest_backup.sha256 || "—"}</dd></div>
              </dl>
            ) : (
              <p className="mt-3 text-sm text-muted">No backup receipt yet. Run a backup cycle before the next migration.</p>
            )}
          </div>

          <div className="card mb-4 p-4">
            <h2 className="font-medium">Latest restore drill</h2>
            {summary.latest_drill ? (
              <dl className="mt-3 grid gap-2 text-sm sm:grid-cols-2">
                <div><dt className="text-muted">Result</dt><dd>{summary.latest_drill.passed ? "Passed" : "Failed"}{summary.latest_drill.sha256_match ? " · SHA-256 match" : ""}</dd></div>
                <div><dt className="text-muted">Created</dt><dd>{summary.latest_drill.created_at ? `${formatDate(summary.latest_drill.created_at)} (${timeAgo(summary.latest_drill.created_at)})` : "—"}</dd></div>
                <div><dt className="text-muted">Tables</dt><dd>{summary.latest_drill.tables_restored} / {summary.latest_drill.tables_expected} restored</dd></div>
                <div><dt className="text-muted">Rows</dt><dd>{summary.latest_drill.rows_matched.toLocaleString()} / {summary.latest_drill.rows_checked.toLocaleString()} matched</dd></div>
                {summary.latest_drill.failures.length > 0 && (
                  <div className="sm:col-span-2"><dt className="text-muted">Failures</dt><dd>{JSON.stringify(summary.latest_drill.failures)}</dd></div>
                )}
              </dl>
            ) : (
              <p className="mt-3 text-sm text-muted">No restore drill has run yet. A backup that was never restored proves nothing.</p>
            )}
          </div>

          <div className="card mb-4 p-4">
            <h2 className="font-medium">Integrity</h2>
            <p className="mt-3 text-sm text-muted">Checked {summary.integrity.checked_at ? `${formatDate(summary.integrity.checked_at)} (${timeAgo(summary.integrity.checked_at)})` : "—"}. Orphans are reported, never deleted here.</p>
            {summary.integrity.unvalidated.length > 0 && (
              <p className="mt-2 text-sm">Unvalidated foreign keys: {summary.integrity.unvalidated.join(", ")}</p>
            )}
            {summary.integrity.orphans.length > 0 && (
              <ul className="mt-2 list-disc pl-5 text-sm">
                {summary.integrity.orphans.map((row) => (
                  <li key={row.constraint}>{row.constraint}: {row.orphans} orphan rows</li>
                ))}
              </ul>
            )}
          </div>

          <div className="card mb-4 p-4">
            <h2 className="font-medium">Migration gate</h2>
            <p className="mt-3 text-sm text-muted">
              Migrations run only through <code>db-migrate-safe</code>, which refuses when no drill-verified backup newer than the last migration exists, or when the newest verified backup is older than the RPO.
            </p>
            {summary.migration_gate.reasons.length > 0 && (
              <ul className="mt-2 list-disc pl-5 text-sm">
                {summary.migration_gate.reasons.map((reason) => (
                  <li key={reason}>{reason}</li>
                ))}
              </ul>
            )}
            <p className="mt-3 text-sm text-muted">{summary.restore_note}</p>
          </div>
        </>
      )}
    </>
  );
}
