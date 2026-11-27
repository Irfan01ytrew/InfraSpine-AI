import { useEffect, useState } from "react";
import { Wallet } from "lucide-react";
import { describeError } from "../api";
import { Alert, Badge, Button, Card, CardBody, CardHeader, Field, SelectInput, Skeleton } from "../components/ui";
import { financeApi } from "./api";
import type { ConnectorMode, ConnectorStatus } from "./api";

/** Administrator settings: which payment connector is active for invoices, and where the choice comes from.
 * The bank URL and token stay in the server environment and are never returned here. */
export function BankConnectorSection() {
  const [status, setStatus] = useState<ConnectorStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState("");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    let mounted = true;
    financeApi.connector().then((value) => {
      if (!mounted) return;
      setStatus(value);
      setSelected(value.admin_mode ?? "");
    }).catch((reason: unknown) => {
      if (mounted) setError(describeError(reason));
    }).finally(() => {
      if (mounted) setLoading(false);
    });
    return () => {
      mounted = false;
    };
  }, []);

  async function save() {
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      const value = await financeApi.setConnector(selected === "" ? null : (selected as ConnectorMode));
      setStatus(value);
      setSelected(value.admin_mode ?? "");
      setSaved(true);
    } catch (reason) {
      setError(describeError(reason));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card className="overflow-hidden" aria-labelledby="bank-connector-title">
      <CardHeader
        icon={<Wallet />}
        title={<span id="bank-connector-title">Payment connector</span>}
        description={<>Which bank connector handles invoice payments, and whether the choice comes from an administrator or the server environment. The mock connector is a test bank — no real money moves.</>}
      />
      <CardBody className="grid gap-4">
        {error && <Alert tone="danger" title="Payment connector could not be loaded">{error}</Alert>}
        {loading ? (
          <Skeleton className="h-24 w-full" />
        ) : status ? (
          <>
            <dl className="grid gap-2 text-sm">
              <div className="flex flex-wrap gap-2">
                <dt className="text-muted">Active connector</dt>
                <dd><b>{status.active ?? "unavailable"}</b> {status.is_mock && <Badge tone="warning">test bank — no real money moves</Badge>}</dd>
              </div>
              <div className="flex flex-wrap gap-2">
                <dt className="text-muted">Choice from</dt>
                <dd>{status.source === "admin" ? "Administrator setting" : "Server environment"}</dd>
              </div>
              <div className="flex flex-wrap gap-2">
                <dt className="text-muted">Environment mode</dt>
                <dd>{status.environment_mode}</dd>
              </div>
              <div className="flex flex-wrap gap-2">
                <dt className="text-muted">Real bank (HTTP)</dt>
                <dd>{status.http_configured ? `configured${status.http_host ? ` — ${status.http_host}` : ""}${status.http_token_set ? ", token set" : ", no token"}` : "not configured"}</dd>
              </div>
              {status.real_bank && typeof status.reachable === "boolean" && (
                <div className="flex flex-wrap gap-2">
                  <dt className="text-muted">Bank reachable</dt>
                  <dd>{status.reachable ? "yes" : `no${status.probe_error ? ` (${status.probe_error})` : ""}`}</dd>
                </div>
              )}
              {status.test_override && (
                <div className="flex flex-wrap gap-2">
                  <dt className="text-muted">Test override</dt>
                  <dd>active — the test connector is forced on</dd>
                </div>
              )}
            </dl>
            {!status.available && status.problem && (
              <Alert tone="warning" title="Connector unavailable">{status.problem}</Alert>
            )}
            {!status.mock_allowed && status.is_mock && (
              <Alert tone="warning" title="Mock connector not allowed here">The mock connector is disabled in production unless it is explicitly allowed.</Alert>
            )}
            <Field label="Administrator choice">
              <SelectInput
                value={selected}
                onChange={(event) => { setSelected(event.target.value); setSaved(false); }}
                aria-label="Payment connector mode"
                disabled={saving}
              >
                <option value="">Use the server environment</option>
                {status.modes.map((mode) => (
                  <option key={mode} value={mode}>{mode}</option>
                ))}
              </SelectInput>
            </Field>
            <div className="flex flex-wrap items-center gap-3">
              <Button onClick={() => void save()} disabled={saving || selected === (status.admin_mode ?? "")}>
                {saving ? "Saving…" : "Save connector"}
              </Button>
              {saved && <span className="text-xs text-muted">Saved — active connector updated.</span>}
            </div>
          </>
        ) : null}
      </CardBody>
    </Card>
  );
}
