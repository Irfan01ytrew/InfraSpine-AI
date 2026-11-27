import { useState } from "react";
import type { FormEvent } from "react";
import { api, describeError } from "../api";
import { formatDate } from "../format";
import { IconAlert, Spinner } from "../icons";
import type { FolderImportProgress } from "../types";
import { FolderInput } from "lucide-react";
import { Card, CardBody, CardHeader } from "./ui";

export function FolderImportProgressSection() {
  const [sourceId, setSourceId] = useState("");
  const [progress, setProgress] = useState<FolderImportProgress | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function refresh(event: FormEvent) {
    event.preventDefault();
    setLoading(true);
    setError(null);
    try {
      setProgress(await api.folderImportProgress(sourceId.trim()));
    } catch (reason) {
      setError(describeError(reason));
      setProgress(null);
    } finally {
      setLoading(false);
    }
  }

  return (
    <Card className="overflow-hidden" aria-labelledby="folder-import-progress-title">
      <CardHeader
        icon={<FolderInput />}
        title={<span id="folder-import-progress-title">Folder import progress</span>}
        description="Follow a large folder import by its source ID. Imports resume after interruptions; file paths are not shown."
      />
      <CardBody className="grid gap-4">
      <form className="form-row folder-progress-form" onSubmit={(event) => void refresh(event)}>
        <label htmlFor="folder-source-id">Source ID</label>
        <input
          id="folder-source-id"
          value={sourceId}
          onChange={(event) => setSourceId(event.target.value)}
          pattern="[A-Za-z0-9._-]{1,80}"
          maxLength={80}
          required
          autoComplete="off"
        />
        <button className="btn btn-secondary" type="submit" disabled={loading || !sourceId.trim()}>
          {loading ? <><Spinner size={14} /> Loading</> : "Refresh"}
        </button>
      </form>
      {error && <div className="inline-alert inline-alert-error" role="alert"><IconAlert size={17} />{error}</div>}
      {progress && (
        <>
          <div className="stats folder-progress-stats" role="status">
            <Stat label="Documents" value={progress.total_documents.toLocaleString()} />
            <Stat label="Ready" value={progress.ready.toLocaleString()} />
            <Stat label="Queued" value={progress.queued.toLocaleString()} />
            <Stat label="Processing" value={progress.processing.toLocaleString()} />
            <Stat label="Failed" value={progress.failed.toLocaleString()} />
            <Stat label="Stored size" value={formatBytes(progress.total_bytes)} />
          </div>
          <p className="muted">Last updated: {progress.last_updated ? formatDate(progress.last_updated) : "No imported files yet"}</p>
        </>
      )}
    </CardBody>
    </Card>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return <div className="stat"><span className="stat-label">{label}</span><span className="stat-value">{value}</span></div>;
}

function formatBytes(bytes: number) {
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}
