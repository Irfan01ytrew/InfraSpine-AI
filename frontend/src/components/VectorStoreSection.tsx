import { useCallback, useEffect, useState } from "react";
import { api, describeError } from "../api";
import { IconAlert, Spinner } from "../icons";
import type { VectorIndex, VectorStoreStatus } from "../types";
import { Database, RefreshCw } from "lucide-react";
import { Button, Card, CardBody, CardHeader } from "./ui";
import { useLiveRefresh } from "../lib/useLiveRefresh";

const INDEX_LABEL: Record<VectorIndex["name"], string> = { documents: "Documents", knowledge: "Org knowledge" };
const INDEX_STATUS: Record<VectorIndex["status"], { label: string; tone: string }> = {
  healthy: { label: "Healthy", tone: "pill-success" },
  out_of_sync: { label: "Syncing", tone: "pill-warning" },
  needs_rebuild: { label: "Needs rebuild", tone: "pill-error" },
  rebuilding: { label: "Rebuilding", tone: "pill-warning" },
};

function bytes(n: number | null): string {
  if (n === null) return "Measured on Qdrant host";
  if (n < 1024) return `${n} B`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MB`;
  return `${(n / 1024 ** 3).toFixed(2)} GB`;
}

/** Settings section: read-only status of the vector backend (admins only; the API enforces it too). */
export function VectorStoreSection() {
  const [status, setStatus] = useState<VectorStoreStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setStatus(await api.vectorStore());
      setError(null);
    } catch (err) {
      setError(describeError(err));
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 5000);
    return () => window.clearInterval(timer);
  }, [load]);
  useLiveRefresh(load);

  const documents = status?.indexes.find((i) => i.name === "documents");

  return (
    <Card className="overflow-hidden" aria-labelledby="vector-store-title">
      <CardHeader
        icon={<Database />}
        title={<span id="vector-store-title">Search index</span>}
        description="Stores the meaning of every document passage so answers can find them quickly."
        actions={<Button size="sm" onClick={() => void load()}><RefreshCw /> Refresh</Button>}
      />
      <CardBody className="grid gap-4">
      {error ? (
        <div className="banner banner-error">
          <IconAlert size={18} /> {error}
        </div>
      ) : !status ? (
        <div className="loading">
          <Spinner size={22} />
        </div>
      ) : (
        <>
          {status.issues.length > 0 && (
            <div className="banner banner-warning" role="status">
              <IconAlert size={18} />
              <span>{status.issues.join(" ")}</span>
            </div>
          )}
          <div className="stats vector-stats">
            <Stat label="Backend" value={status.backend.toUpperCase()} hint={status.index_type} />
            <Stat
              label="Status"
              value={status.status === "healthy" ? "Healthy" : "Degraded"}
              hint={`Supported: ${status.supported_backends.join(", ")}`}
            />
            <Stat label="Dimension" value={String(status.dimension)} hint={status.embedding_model} />
            <Stat
              label="Vectors"
              value={(documents?.vectors ?? 0).toLocaleString()}
              hint={`${(documents?.expected_vectors ?? 0).toLocaleString()} in database`}
            />
          </div>
          <table className="trace-chunks vector-indexes">
            <thead>
              <tr>
                <th>Index</th>
                <th>Status</th>
                <th>Vectors</th>
                <th>Database</th>
                <th>On disk</th>
                <th>Memory</th>
              </tr>
            </thead>
            <tbody>
              {status.indexes.map((index) => (
                <tr key={index.name}>
                  <td>{INDEX_LABEL[index.name]}</td>
                  <td>
                    <span className={`pill pill-sm ${INDEX_STATUS[index.status].tone}`}>{INDEX_STATUS[index.status].label}</span>
                  </td>
                  <td>
                    {index.vectors.toLocaleString()}
                    {index.status === "rebuilding" && index.expected_vectors > 0 && (
                      <progress
                        className="vector-rebuild-progress"
                        value={Math.min(index.vectors, index.expected_vectors)}
                        max={index.expected_vectors}
                        aria-label={`Rebuilt ${index.vectors} of ${index.expected_vectors} vectors`}
                      />
                    )}
                  </td>
                  <td>{index.expected_vectors.toLocaleString()}</td>
                  <td>{index.persisted ? index.snapshot_bytes === null || index.change_log_bytes === null ? "Qdrant managed" : bytes(index.snapshot_bytes + index.change_log_bytes) : "Not saved yet"}</td>
                  <td>{bytes(index.memory_bytes)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </CardBody>
    </Card>
  );
}

function Stat({ label, value, hint }: { label: string; value: string; hint: string }) {
  return (
    <div className="stat">
      <span className="stat-label">{label}</span>
      <span className="stat-value">{value}</span>
      <span className="stat-hint">{hint}</span>
    </div>
  );
}
