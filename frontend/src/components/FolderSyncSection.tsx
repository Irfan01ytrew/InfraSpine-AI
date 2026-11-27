import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";
import { api, describeError } from "../api";
import { formatDate } from "../format";
import { IconAlert, Spinner } from "../icons";
import type { DepartmentGroup, FolderSyncRun, FolderSyncSource } from "../types";
import { FolderSync, RefreshCw } from "lucide-react";
import { Button, Card, CardBody, CardHeader, SelectInput } from "./ui";
import { useLiveRefresh } from "../lib/useLiveRefresh";

export function FolderSyncSection() {
  const [sources, setSources] = useState<FolderSyncSource[]>([]);
  const [departments, setDepartments] = useState<DepartmentGroup[]>([]);
  const [name, setName] = useState("");
  const [path, setPath] = useState("");
  const [visibility, setVisibility] = useState<"private" | "shared" | "restricted">("restricted");
  const [departmentId, setDepartmentId] = useState("");
  const [interval, setInterval] = useState("60");
  const [historySource, setHistorySource] = useState<string | null>(null);
  const [runs, setRuns] = useState<FolderSyncRun[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [sourceResponse, groupResponse] = await Promise.all([api.folderSyncSources(), api.adminGroups()]);
      setSources(sourceResponse.items);
      setDepartments(groupResponse.groups.filter((group) => group.kind === "department" && group.source === "local"));
      setError("");
    } catch (reason) {
      setError(describeError(reason));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);
  useLiveRefresh(refresh);

  async function create(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await api.createFolderSyncSource({
        name: name.trim(),
        relative_path: path.trim(),
        visibility,
        department_group_id: visibility === "restricted" ? departmentId : null,
        interval_minutes: Number(interval),
      });
      setName("");
      setPath("");
      setNotice("Scheduled folder source created. The worker will pick it up on its next poll.");
      await refresh();
    } catch (reason) {
      setError(describeError(reason));
    } finally {
      setBusy(false);
    }
  }

  async function toggle(source: FolderSyncSource) {
    setBusy(true);
    setError("");
    try {
      await api.updateFolderSyncSource(source.id, { enabled: !source.enabled });
      await refresh();
    } catch (reason) {
      setError(describeError(reason));
    } finally {
      setBusy(false);
    }
  }

  async function showHistory(source: FolderSyncSource) {
    setBusy(true);
    setError("");
    try {
      const response = await api.folderSyncRuns(source.id);
      setRuns(response.items);
      setHistorySource(source.id);
    } catch (reason) {
      setError(describeError(reason));
    } finally {
      setBusy(false);
    }
  }

  return <Card className="overflow-hidden" aria-labelledby="folder-sync-title">
    <CardHeader
      icon={<FolderSync />}
      title={<span id="folder-sync-title">Scheduled folder synchronization</span>}
      description="Keep a shared folder in step automatically. Folders are only read, and must be inside the server's allowed import folder."
      actions={<Button size="sm" onClick={() => void refresh()} disabled={loading}><RefreshCw /> Refresh</Button>}
    />
      <CardBody className="grid gap-4">
    {error && <div className="inline-alert inline-alert-error" role="alert"><IconAlert size={17} />{error}</div>}
    {notice && <div className="inline-alert" role="status">{notice}</div>}
    <form className="folder-sync-form" onSubmit={(event) => void create(event)}>
      <label>Source name<input value={name} onChange={(event) => setName(event.target.value)} maxLength={120} required /></label>
      <label>Folder path relative to import root<input value={path} onChange={(event) => setPath(event.target.value)} maxLength={1024} placeholder="engineering/handbooks" required /></label>
      <label>Document access<SelectInput value={visibility} onChange={(event) => setVisibility(event.target.value as typeof visibility)}>
        <option value="restricted">Department restricted</option><option value="private">Private to Admin owner</option><option value="shared">Shared with signed-in users</option>
      </SelectInput></label>
      {visibility === "restricted" && <label>Department<SelectInput value={departmentId} onChange={(event) => setDepartmentId(event.target.value)} required>
        <option value="">Choose a department</option>{departments.map((department) => <option key={department.id} value={department.id}>{department.name}</option>)}
      </SelectInput></label>}
      <label>Sync interval (minutes)<input type="number" min={5} max={10080} value={interval} onChange={(event) => setInterval(event.target.value)} required /></label>
      <div className="people-row-actions"><button className="btn btn-primary" type="submit" disabled={busy || !name.trim() || !path.trim() || (visibility === "restricted" && !departmentId)}>
        {busy ? <><Spinner size={14} /> Saving</> : "Add scheduled source"}
      </button></div>
    </form>
    {loading ? <div className="loading"><Spinner size={20} /></div> : sources.length === 0 ? <p className="empty">No scheduled sources yet.</p> : <div className="table-wrap"><table>
      <thead><tr><th>Source</th><th>Access</th><th>Schedule</th><th>Last run</th><th>Action</th></tr></thead>
      <tbody>{sources.map((source) => <tr key={source.id}>
        <td><strong>{source.name}</strong><div className="muted">{source.relative_path}</div></td>
        <td>{source.visibility}{source.department_group_id ? ` · ${departments.find((group) => group.id === source.department_group_id)?.name ?? "Department"}` : ""}</td>
        <td>{source.enabled ? `Every ${source.interval_minutes} min` : "Paused"}{source.consecutive_failures > 0 && <div className="inline-alert inline-alert-error">Retry {source.consecutive_failures}: {source.last_error_code ?? "scan issue"}</div>}</td>
        <td>{source.last_run_at ? formatDate(source.last_run_at) : "Not run yet"}</td>
        <td className="table-actions"><button className="btn btn-ghost" disabled={busy} onClick={() => void showHistory(source)}>Run history</button><button className="btn" disabled={busy} onClick={() => void toggle(source)}>{source.enabled ? "Pause" : "Resume"}</button></td>
      </tr>)}</tbody>
    </table></div>}
    {historySource && <div className="folder-sync-history" aria-live="polite">
      <h3>Recent run history</h3>
      {runs.length === 0 ? <p className="empty">No runs recorded.</p> : <ul className="grid">{runs.map((run) => <li className="border-t border-border py-2.5 first:border-t-0" key={run.id}>
        <div className="grid gap-0.5"><strong className="text-[13px] font-medium">{run.status}</strong><small className="text-xs text-muted">{formatDate(run.started_at)} · scanned {run.scanned} · added {run.created} · updated {run.updated} · removed {run.removed} · failed {run.failed}{run.error_code ? ` · ${run.error_code}` : ""}</small></div>
      </li>)}</ul>}
    </div>}
  </CardBody>
    </Card>;
}
