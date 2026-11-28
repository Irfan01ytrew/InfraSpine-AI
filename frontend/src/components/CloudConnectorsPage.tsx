import { useCallback, useEffect, useState } from "react";
import type { ReactNode } from "react";
import { api, describeError } from "../api";
import { formatDate } from "../format";
import type { CloudConnector, CloudConnectorCreateInput, CloudConnectorType, DepartmentGroup } from "../types";
import { AlertTriangle, Building2, Cloud, HardDrive, Lock, Plus, RefreshCw, Share2, Trash2 } from "lucide-react";
import { cn } from "../lib/utils";
import { useFeedback } from "./Feedback";
import { Alert, Badge, Button, Card, CardBody, CardHeader, EmptyState, Input, PageHeader, SelectInput, Skeleton, Stat } from "./ui";
import { useLiveRefresh } from "../lib/useLiveRefresh";

const blankForm: CloudConnectorCreateInput = {
  name: "",
  connector_type: "google_drive",
  client_id: "",
  client_secret: "",
  refresh_token: "",
  drive_id: "",
  folder_id: "",
  visibility: "restricted",
  sync_interval_minutes: 60,
  department_group_id: "",
};

const providerLabel: Record<CloudConnectorType, string> = {
  google_drive: "Google Drive",
  onedrive: "OneDrive",
  sharepoint: "SharePoint",
};

export function CloudConnectorsPage() {
  const { confirm } = useFeedback();
  const [items, setItems] = useState<CloudConnector[]>([]);
  const [groups, setGroups] = useState<DepartmentGroup[]>([]);
  const [form, setForm] = useState<CloudConnectorCreateInput>(blankForm);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const refresh = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    try {
      const [connectors, accessGroups] = await Promise.all([api.cloudConnectors(), api.adminGroups()]);
      setItems(connectors);
      setGroups(accessGroups.groups);
      setError(null);
    } catch (err) {
      setError(describeError(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);
  useLiveRefresh(() => refresh(true));

  function setField<K extends keyof CloudConnectorCreateInput>(key: K, value: CloudConnectorCreateInput[K]) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  async function create(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      await api.createCloudConnector({
        ...form,
        name: form.name.trim(),
        client_id: form.client_id.trim(),
        client_secret: form.client_secret,
        ...(form.tenant_id ? { tenant_id: form.tenant_id.trim() } : {}),
        ...(form.refresh_token ? { refresh_token: form.refresh_token } : {}),
        ...(form.drive_id ? { drive_id: form.drive_id.trim() } : {}),
        ...(form.folder_id ? { folder_id: form.folder_id.trim() } : {}),
      department_group_id: form.department_group_id,
      });
      setForm({ ...blankForm });
      setNotice("Connector saved. Provider access will be checked when you start a sync.");
      await refresh();
    } catch (err) {
      setError(describeError(err));
    } finally {
      setSaving(false);
    }
  }

  async function sync(item: CloudConnector) {
    if (!await confirm({ title: `Sync “${item.name}” now?`, body: `This contacts ${providerLabel[item.connector_type]} using the saved credentials.`, confirmLabel: "Start sync" })) return;
    setBusyId(item.id);
    setError(null);
    setNotice(null);
    try {
      const result = await api.syncCloudConnector(item.id);
      setNotice(
        `Sync ${result.status}: ${result.files_synced} downloaded, ${result.files_queued} queued for processing, ` +
        `${result.files_unchanged} unchanged, ${result.files_skipped} unsupported, ${result.files_deleted} removed; ` +
        `${result.errors.length} errors.` +
        (typeof result.permission_reconciliation_complete !== "boolean"
          ? ""
          : ` Permission scan: ${result.permission_reconciliation_scanned} checked, ` +
            `${result.permission_reconciliation_updated} refreshed, ` +
            `${result.permission_reconciliation_quarantined} quarantined, ` +
            `${result.permission_reconciliation_failed} deferred; ` +
            `${result.permission_reconciliation_complete ? "library pass complete" : "next bounded page saved"}.`),
      );
      await refresh();
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusyId(null);
    }
  }

  async function remove(item: CloudConnector) {
    if (!await confirm({ title: `Remove “${item.name}”?`, body: "Its encrypted credentials are permanently deleted. Imported originals are kept and their access is quarantined.", confirmLabel: "Remove connector", tone: "danger" })) return;
    setBusyId(item.id);
    setError(null);
    setNotice(null);
    try {
      await api.deleteCloudConnector(item.id);
      setNotice("Connector and encrypted credentials were removed. Imported originals were retained and access was quarantined.");
      await refresh();
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusyId(null);
    }
  }

  const isMicrosoft = form.connector_type !== "google_drive";
  const departments = groups.filter((group) => group.kind === "department" && group.lifecycle_status !== "archived");
  const syncing = items.filter((item) => item.sync_in_progress).length;
  const withErrors = items.filter((item) => item.last_sync_error_count > 0).length;

  return (
    <section className="cloud-connectors-page">
      <PageHeader
        title="Cloud connectors"
        description="Bring files from Google Drive, OneDrive or SharePoint into the knowledge base, with their sharing permissions."
        actions={<Button onClick={() => void refresh()} loading={loading}>{!loading && <RefreshCw />} Refresh</Button>}
      />
      <div className="mb-5 grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat icon={<Cloud />} label="Connectors" value={loading ? "…" : items.length} />
        <Stat icon={<RefreshCw />} label="Syncing now" value={loading ? "…" : syncing} />
        <Stat icon={<AlertTriangle />} label="Last sync had errors" value={<span className={withErrors ? "text-danger-text" : undefined}>{loading ? "…" : withErrors}</span>} />
        <Stat icon={<Building2 />} label="Departments available" value={departments.length} />
      </div>

      <Alert tone="warning" className="mb-5" title="How connector security works">
        <ul className="mt-1 grid list-disc gap-1 pl-4">
          <li>Credentials are encrypted at rest and never shown again. Saving does not contact the provider; a sync does, and asks first.</li>
          <li>Imported copies are limited to the chosen department and to the provider’s mapped user and group permissions.</li>
          <li>Each sync rechecks changed files plus one bounded page of the full library. Files missing from a page are left alone.</li>
          <li>Link, domain, unknown or unmapped sharing fails closed and quarantines the file. The provider identity/group mapping still needs validation with your directory.</li>
        </ul>
      </Alert>
      {error && <Alert tone="danger" className="mb-5"><span className="cloud-connectors-message cloud-connectors-error" role="alert">{error}</span></Alert>}
      {notice && <Alert tone="success" className="mb-5"><span className="cloud-connectors-message">{notice}</span></Alert>}

      <div className="grid items-start gap-5 xl:grid-cols-[minmax(0,420px)_minmax(0,1fr)]">
        <Card className="overflow-hidden">
          <CardHeader icon={<Plus />} title="Add a connector" description="You need an app registration (client ID and secret) from the provider’s admin console." />
          <CardBody>
            <form className="cloud-connector-form grid gap-3" onSubmit={(event) => void create(event)}>
              <div className="grid grid-cols-3 gap-2" role="radiogroup" aria-label="Provider">
                {(Object.keys(providerLabel) as CloudConnectorType[]).map((type) => (
                  <button
                    key={type}
                    type="button"
                    role="radio"
                    aria-checked={form.connector_type === type}
                    onClick={() => setForm((current) => ({ ...current, connector_type: type, tenant_id: "", refresh_token: "" }))}
                    className={cn("grid cursor-pointer place-items-center gap-1 rounded-lg border px-2 py-3 text-xs font-medium outline-none transition-colors focus-visible:ring-[3px] focus-visible:ring-ring", form.connector_type === type ? "border-primary bg-primary-soft text-primary" : "border-border text-foreground-2 hover:border-border-strong")}
                  >
                    {type === "google_drive" ? <HardDrive className="size-5" /> : type === "onedrive" ? <Cloud className="size-5" /> : <Share2 className="size-5" />}
                    {providerLabel[type]}
                  </button>
                ))}
              </div>
              <FormField label="Name"><Input required maxLength={120} value={form.name} onChange={(event) => setField("name", event.target.value)} autoComplete="off" placeholder={`e.g. Finance ${providerLabel[form.connector_type]}`} /></FormField>
              <div className="grid gap-3 sm:grid-cols-2">
                <FormField label="Client ID"><Input required maxLength={512} value={form.client_id} onChange={(event) => setField("client_id", event.target.value)} autoComplete="off" /></FormField>
                <FormField label="Client secret"><Input required type="password" maxLength={512} value={form.client_secret} onChange={(event) => setField("client_secret", event.target.value)} autoComplete="new-password" /></FormField>
              </div>
              {isMicrosoft ? (
                <FormField label="Tenant ID"><Input required maxLength={128} value={form.tenant_id ?? ""} onChange={(event) => setField("tenant_id", event.target.value)} autoComplete="off" /></FormField>
              ) : (
                <FormField label="Refresh token"><Input required type="password" maxLength={2048} value={form.refresh_token ?? ""} onChange={(event) => setField("refresh_token", event.target.value)} autoComplete="new-password" /></FormField>
              )}
              <div className="grid gap-3 sm:grid-cols-2">
                <FormField label="Drive ID" optional><Input maxLength={256} value={form.drive_id ?? ""} onChange={(event) => setField("drive_id", event.target.value)} autoComplete="off" /></FormField>
                <FormField label="Folder ID" optional><Input maxLength={256} value={form.folder_id ?? ""} onChange={(event) => setField("folder_id", event.target.value)} autoComplete="off" /></FormField>
              </div>
              <div className="grid gap-3 sm:grid-cols-2">
                <FormField label="Department access">
                  <SelectInput required value={form.department_group_id ?? ""} onChange={(event) => setField("department_group_id", event.target.value)}>
                    <option value="">Choose a department</option>
                    {departments.map((group) => <option key={group.id} value={group.id}>{group.name}</option>)}
                  </SelectInput>
                </FormField>
                <FormField label="Sync interval (minutes)"><Input type="number" required min={5} max={10080} value={form.sync_interval_minutes} onChange={(event) => setField("sync_interval_minutes", Number(event.target.value))} /></FormField>
              </div>
              <Button type="submit" variant="primary" loading={saving} disabled={saving || !form.department_group_id}>{!saving && <Lock />} {saving ? "Saving…" : "Save connector"}</Button>
            </form>
          </CardBody>
        </Card>

        <Card className="cloud-connector-list min-w-0 overflow-hidden">
          <CardHeader icon={<Cloud />} title="Saved connectors" actions={<Badge>{items.length}</Badge>} />
          {loading ? <CardBody className="grid gap-3"><Skeleton className="h-20" /><Skeleton className="h-20" /></CardBody> : items.length === 0 ? (
            <EmptyState icon={<Cloud />} title="No cloud connectors yet" description="Add one to keep a shared drive or SharePoint library in the knowledge base." />
          ) : (
            <div className="grid gap-3 p-4">
              {items.map((item) => (
                <article className="cloud-connector-card flex flex-col gap-3 rounded-lg border border-border p-4 sm:flex-row sm:items-center sm:justify-between" key={item.id}>
                  <div className="flex min-w-0 items-start gap-3">
                    <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-primary-soft text-primary">{item.connector_type === "google_drive" ? <HardDrive className="size-[18px]" /> : item.connector_type === "onedrive" ? <Cloud className="size-[18px]" /> : <Share2 className="size-[18px]" />}</span>
                    <div className="grid min-w-0 gap-1">
                      <h3 className="truncate text-sm font-semibold text-foreground">{item.name}</h3>
                      <p className="text-xs text-muted">{providerLabel[item.connector_type]} · {groups.find((group) => group.id === item.department_group_id)?.name ?? "Department unavailable"} · every {item.sync_interval_minutes} min</p>
                      <div className="flex flex-wrap items-center gap-1.5">
                        <Badge tone={item.sync_in_progress ? "primary" : item.last_sync_error_count > 0 ? "danger" : item.last_sync_status ? "success" : "neutral"} dot>
                          {item.sync_in_progress ? "Syncing" : item.last_sync_status ? `Last sync: ${item.last_sync_status}` : "Not yet synced"}
                        </Badge>
                        {item.last_sync_error_count > 0 && <Badge tone="danger">{item.last_sync_error_count} errors</Badge>}
                        {item.last_synced_at && <span className="text-xs text-muted">{formatDate(item.last_synced_at)}</span>}
                      </div>
                    </div>
                  </div>
                  <div className="flex shrink-0 gap-2">
                    <Button size="sm" loading={busyId === item.id} onClick={() => void sync(item)} disabled={busyId === item.id || item.sync_in_progress}>{busyId !== item.id && <RefreshCw />} Sync now</Button>
                    <Button size="sm" variant="danger-ghost" onClick={() => void remove(item)} disabled={busyId === item.id || item.sync_in_progress}><Trash2 /> Remove</Button>
                  </div>
                </article>
              ))}
            </div>
          )}
        </Card>
      </div>
    </section>
  );
}

/** Label wrapper that keeps the `.field-label` hook the connector acceptance script looks up. */
function FormField({ label, optional = false, children }: { label: string; optional?: boolean; children: ReactNode }) {
  return (
    <label className="grid min-w-0 gap-1.5">
      <span className="field-label text-xs font-medium text-foreground-2">{label}{optional && <span className="font-normal text-muted"> (optional)</span>}</span>
      {children}
    </label>
  );
}
