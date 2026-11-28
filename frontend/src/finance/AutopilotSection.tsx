import { useEffect, useState } from "react";
import { Bot } from "lucide-react";
import { describeError } from "../api";
import { Alert, Badge, Button, Card, CardBody, CardHeader, Checkbox, Field, Input, Skeleton } from "../components/ui";
import { financeApi } from "./api";
import type { AutopilotStatus } from "./api";

/** Administrator settings: the finance autopilot policy and its last sweep.
 * The sweep only starts waiting invoice batches on the worker schedule. It never
 * approves a payment, never pays, and never calls the bank — a human still
 * approves the exact payment and treasury still dispatches it. */
export function AutopilotSection() {
  const [status, setStatus] = useState<AutopilotStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [enabled, setEnabled] = useState(false);
  const [dryRun, setDryRun] = useState(false);
  const [maxAmount, setMaxAmount] = useState("");
  const [dailyCap, setDailyCap] = useState("");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    let mounted = true;
    financeApi.autopilotStatus().then((value) => {
      if (!mounted) return;
      setStatus(value);
      setEnabled(value.policy.enabled);
      setDryRun(value.policy.dry_run);
      setMaxAmount(value.policy.max_payment_amount ?? "");
      setDailyCap(value.policy.daily_cap ?? "");
    }).catch((reason: unknown) => {
      if (mounted) setError(describeError(reason));
    }).finally(() => {
      if (mounted) setLoading(false);
    });
    return () => {
      mounted = false;
    };
  }, []);

  const changed = status !== null && (
    enabled !== status.policy.enabled || dryRun !== status.policy.dry_run ||
    maxAmount !== (status.policy.max_payment_amount ?? "") || dailyCap !== (status.policy.daily_cap ?? "")
  );

  async function save() {
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      const value = await financeApi.setAutopilotPolicy({
        enabled, dry_run: dryRun,
        max_payment_amount: maxAmount.trim() === "" ? null : maxAmount.trim(),
        daily_cap: dailyCap.trim() === "" ? null : dailyCap.trim(),
      });
      setStatus(value);
      setEnabled(value.policy.enabled);
      setDryRun(value.policy.dry_run);
      setMaxAmount(value.policy.max_payment_amount ?? "");
      setDailyCap(value.policy.daily_cap ?? "");
      setSaved(true);
    } catch (reason) {
      setError(describeError(reason));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card className="overflow-hidden" aria-labelledby="autopilot-title">
      <CardHeader
        icon={<Bot />}
        title={<span id="autopilot-title">Finance autopilot</span>}
        description={<>Picks up waiting invoice batches on the worker schedule and wakes runs stalled on an expired approval. It never approves or pays — payments still need a human approval.</>}
      />
      <CardBody className="grid gap-4">
        {error && <Alert tone="danger" title="Autopilot settings could not be loaded">{error}</Alert>}
        {loading ? (
          <Skeleton className="h-24 w-full" />
        ) : status ? (
          <>
            <dl className="grid gap-2 text-sm">
              <div className="flex flex-wrap gap-2">
                <dt className="text-muted">Status</dt>
                <dd>{status.policy.enabled
                  ? <><b>Enabled</b> {status.policy.dry_run && <Badge tone="warning">dry run — decides but starts nothing</Badge>}</>
                  : "Disabled — batches only move when a person starts them"}</dd>
              </div>
              <div className="flex flex-wrap gap-2">
                <dt className="text-muted">Last sweep</dt>
                <dd>{status.last_sweep
                  ? <>{new Date(status.last_sweep.at).toLocaleString()}{status.last_sweep.actor_email ? ` · as ${status.last_sweep.actor_email}` : ""} · {status.last_sweep.decisions.length} decision(s)</>
                  : "No sweep has run yet"}</dd>
              </div>
              {status.last_sweep && status.last_sweep.decisions.length > 0 && (
                <div className="flex flex-wrap gap-2">
                  <dt className="text-muted">Decisions</dt>
                  <dd><ul className="grid gap-1">{status.last_sweep.decisions.map((decision) => (
                    <li key={`${decision.batch_id}-${decision.action}`}><span className="font-mono">{decision.batch_id.slice(0, 8)}</span> — <b>{decision.action}</b> · {decision.reason}</li>
                  ))}</ul></dd>
                </div>
              )}
            </dl>
            <label className="flex items-start gap-2 text-sm">
              <Checkbox checked={enabled} onCheckedChange={(value) => { setEnabled(value === true); setSaved(false); }} className="mt-0.5" />
              <span>Enable the autopilot sweep <span className="text-muted">(batches are picked up on the worker schedule)</span></span>
            </label>
            <label className="flex items-start gap-2 text-sm">
              <Checkbox checked={dryRun} onCheckedChange={(value) => { setDryRun(value === true); setSaved(false); }} className="mt-0.5" />
              <span>Dry run <span className="text-muted">(report what would start, change nothing)</span></span>
            </label>
            <Field label="Maximum payment per vendor (PKR, blank = no limit)">
              <Input value={maxAmount} onChange={(event) => { setMaxAmount(event.target.value); setSaved(false); }} inputMode="decimal" placeholder="e.g. 500000.00" disabled={saving} />
            </Field>
            <Field label="Daily cap per department (PKR, blank = no limit)">
              <Input value={dailyCap} onChange={(event) => { setDailyCap(event.target.value); setSaved(false); }} inputMode="decimal" placeholder="e.g. 2000000.00" disabled={saving} />
            </Field>
            {enabled && !dryRun && (
              <Alert tone="warning" title="Live sweep">Batches over either limit are skipped and left for a human. The sweep still cannot approve or pay anything.</Alert>
            )}
            <div className="flex flex-wrap items-center gap-3">
              <Button onClick={() => void save()} disabled={saving || !changed}>
                {saving ? "Saving…" : "Save autopilot policy"}
              </Button>
              {saved && <span className="text-xs text-muted">Saved — policy updated.</span>}
            </div>
          </>
        ) : null}
      </CardBody>
    </Card>
  );
}
