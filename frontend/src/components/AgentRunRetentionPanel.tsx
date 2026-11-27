import { useState } from "react";
import { api, describeError } from "../api";
import { formatDay } from "../format";
import type { AgentRunRetentionPreview, AgentRunRetentionPurgeResult } from "../types";
import { Archive, Search, Trash2 } from "lucide-react";
import { Alert, Button, Card, CardBody, CardHeader, Field, Input, Select } from "./ui";

export function AgentRunRetentionPanel() {
  const [retentionDays, setRetentionDays] = useState("365");
  const [batchSize, setBatchSize] = useState("100");
  const [confirmation, setConfirmation] = useState("");
  const [preview, setPreview] = useState<AgentRunRetentionPreview | null>(null);
  const [lastPurge, setLastPurge] = useState<AgentRunRetentionPurgeResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const days = Number(retentionDays);
  const batch = Number(batchSize);
  const canPreview = Number.isInteger(days) && days >= 30 && days <= 3650;
  const canPurge = preview?.dry_run === true && preview.retention_days === days && preview.eligible_runs > 0 &&
    Number.isInteger(batch) && batch >= 1 && batch <= 1000 && confirmation === "PURGE";

  function clearPreview() {
    setPreview(null);
    setLastPurge(null);
    setConfirmation("");
  }

  async function runPreview() {
    setBusy(true);
    setError(null);
    setLastPurge(null);
    setConfirmation("");
    try {
      setPreview(await api.previewAgentRunRetention(days));
    } catch (err) {
      setPreview(null);
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  }

  async function purgeBatch() {
    if (!canPurge) return;
    setBusy(true);
    setError(null);
    try {
      const result = await api.purgeAgentRunRetention({ retention_days: days, batch_size: batch, confirm: true });
      setLastPurge(result);
      setPreview(null);
      setConfirmation("");
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card className="mt-6" aria-labelledby="agent-retention-title">
      <CardHeader
        icon={<Archive />}
        title={<span id="agent-retention-title">Agent history cleanup</span>}
        description="Delete the questions and answers of old finished agent runs to save space and limit stored data. Run dates, status and the audit trail are always kept."
      />
      <CardBody className="grid gap-4">
        {error && <Alert tone="danger">{error}</Alert>}
        <form className="grid items-end gap-4 md:grid-cols-[1fr_1fr_auto]" onSubmit={(event) => { event.preventDefault(); void runPreview(); }}>
          <Field label="Keep runs from the last">
            <Select aria-label="Retention days" value={retentionDays} onValueChange={(value) => { setRetentionDays(value); clearPreview(); }} options={[{ value: "90", label: "90 days" }, { value: "180", label: "6 months" }, { value: "365", label: "1 year" }, { value: "730", label: "2 years" }, { value: "1825", label: "5 years" }]} />
          </Field>
          <Field label="Delete at most per round">
            <Select aria-label="Batch size" value={batchSize} onValueChange={(value) => { setBatchSize(value); setLastPurge(null); setConfirmation(""); }} options={[{ value: "50", label: "50 runs" }, { value: "100", label: "100 runs" }, { value: "500", label: "500 runs" }, { value: "1000", label: "1,000 runs" }]} />
          </Field>
          <Button type="submit" loading={busy && !preview} disabled={busy || !canPreview}><Search /> Check what would be deleted</Button>
        </form>
        <p className="text-xs text-muted">Only finished or failed runs older than this are affected; queued and running ones are never touched. Nothing is deleted until you confirm.</p>
        {preview && (
          <div className="grid gap-3 rounded-lg border border-border bg-surface-2/50 p-4" role="status" aria-live="polite">
            <p className="text-[13px] text-foreground"><strong>{preview.eligible_runs}</strong> run{preview.eligible_runs === 1 ? "" : "s"} finished before {formatDay(preview.cutoff_at)} can be cleaned up.</p>
            {preview.eligible_runs > 0 ? <>
              <Alert tone="warning">Deleted content cannot be recovered. Kept: {preview.preserved_fields.join(", ")}.</Alert>
              <div className="grid items-end gap-3 md:grid-cols-[1fr_auto]">
                <Field label={<>Type <strong>PURGE</strong> to confirm</>}><Input aria-label="Type PURGE to confirm" autoComplete="off" value={confirmation} onChange={(event) => setConfirmation(event.target.value)} placeholder="PURGE" /></Field>
                <Button variant="danger" loading={busy} disabled={busy || !canPurge} onClick={() => void purgeBatch()}><Trash2 /> Delete up to {batch} run{batch === 1 ? "" : "s"}</Button>
              </div>
            </> : <p className="text-xs text-muted">Nothing to clean up yet.</p>}
          </div>
        )}
        {lastPurge && <Alert tone="success">Cleaned up {lastPurge.purged_runs} run{lastPurge.purged_runs === 1 ? "" : "s"}; {lastPurge.remaining_eligible_runs} still eligible. Check again before the next round. Recorded in the audit log.</Alert>}
      </CardBody>
    </Card>
  );
}
