import { useCallback, useEffect, useState } from "react";
import { Download, Send, UserPlus } from "lucide-react";
import { api, describeError } from "../api";
import type { DepartmentGroup, UserInvitation } from "../types";
import { useFeedback } from "./Feedback";
import { Alert, Badge, Button, Card, CardBody, CardHeader, EmptyState, Field, PageHeader, Select, Textarea } from "./ui";
import { useLiveRefresh } from "../lib/useLiveRefresh";

const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const MAX = 200;

/** For accounts holding people.invite (e.g. HR): invite Employees into the departments in their scope. */
export function InviteEmployeesPage() {
  const { toast, confirm } = useFeedback();
  const [departments, setDepartments] = useState<DepartmentGroup[] | null>(null);
  const [departmentId, setDepartmentId] = useState("");
  const [pending, setPending] = useState<UserInvitation[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [results, setResults] = useState<{ email: string; link?: string; error?: string }[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    api.invitableDepartments()
      .then((response) => {
        setDepartments(response.groups);
        setDepartmentId((current) => current || response.groups[0]?.id || "");
      })
      .catch((err) => { setDepartments([]); setLoadError(describeError(err)); });
  }, []);

  const loadPending = useCallback(async () => {
    if (!departmentId) return;
    try { setPending((await api.departmentInvitations(departmentId)).invitations); } catch { setPending([]); }
  }, [departmentId]);
  useEffect(() => { void loadPending(); }, [loadPending]);
  useLiveRefresh(loadPending);

  const tokens = [...new Set(text.split(/[\s,;]+/).map((item) => item.trim().toLowerCase()).filter(Boolean))];
  const valid = tokens.filter((item) => EMAIL.test(item));
  const invalid = tokens.filter((item) => !EMAIL.test(item));
  const departmentName = departments?.find((group) => group.id === departmentId)?.name ?? "";

  function link(token: string | null | undefined) {
    return `${window.location.origin}${window.location.pathname}${window.location.search}#/onboarding?token=${encodeURIComponent(token ?? "")}`;
  }

  async function invite(event: React.FormEvent) {
    event.preventDefault();
    const emails = valid.slice(0, MAX);
    if (!emails.length || !departmentId) return;
    const ok = await confirm({
      title: `Invite ${emails.length} ${emails.length === 1 ? "employee" : "employees"} to ${departmentName}?`,
      body: "Each person gets a single-use sign-up link that expires in 72 hours. Links are not emailed: download them and send them through your approved channel.",
      confirmLabel: "Create invitations",
    });
    if (!ok) return;
    setBusy(true);
    const queue = [...emails];
    const done: { email: string; link?: string; error?: string }[] = [];
    const worker = async () => {
      for (let email = queue.shift(); email; email = queue.shift()) {
        try { done.push({ email, link: link((await api.createDepartmentInvitation(departmentId, email)).token) }); }
        catch (err) { done.push({ email, error: describeError(err) }); }
        setResults([...done]);
      }
    };
    await Promise.all(Array.from({ length: Math.min(4, emails.length) }, worker));
    setBusy(false);
    const created = done.filter((row) => row.link).length;
    toast(created === emails.length ? "success" : "error", `${created} of ${emails.length} invitations created`);
    if (created) setText("");
    await loadPending();
  }

  function downloadLinks() {
    const rows = [["email", "department", "invitation_link"], ...results.filter((row) => row.link).map((row) => [row.email, departmentName, row.link!])];
    const csv = rows.map((row) => row.map((value) => (/[",\n]/.test(value) ? `"${value.replaceAll('"', '""')}"` : value)).join(",")).join("\r\n") + "\r\n";
    const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "invitation-links.csv";
    anchor.click();
    URL.revokeObjectURL(url);
  }

  return (
    <>
      <PageHeader title="Invite employees" description="Add new employees to the departments you are responsible for. They sign up with a single-use link and join the selected department." />
      {loadError && <Alert tone="danger" className="mb-4">{loadError}</Alert>}
      {departments !== null && departments.length === 0 ? (
        <Card><EmptyState icon={<UserPlus />} title="No departments to invite into" description="An administrator can give you the “Invite employees” permission for one or more departments in People & access → Permissions." /></Card>
      ) : (
        <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_340px]">
          <Card>
            <CardHeader icon={<UserPlus />} title="New employees" description="Paste work emails separated by new lines, commas or spaces." />
            <CardBody>
              <form className="grid gap-4" onSubmit={(event) => void invite(event)}>
                <Field label="Department">
                  <Select aria-label="Department" value={departmentId} onValueChange={setDepartmentId} options={(departments ?? []).map((group) => ({ value: group.id, label: group.name }))} placeholder="Choose a department" />
                </Field>
                <Field label="Work emails" hint={`Up to ${MAX} at a time. Everyone joins as an Employee.`}>
                  <Textarea rows={5} value={text} onChange={(event) => setText(event.target.value)} placeholder={"ayesha.khan@yourcompany.com\nbilal.ahmed@yourcompany.com"} />
                </Field>
                {text.trim() && <div className="flex flex-wrap gap-2" role="status">
                  <Badge tone="success" dot>{valid.length} ready</Badge>
                  {invalid.length > 0 && <Badge tone="danger" dot>{invalid.length} not an email</Badge>}
                </div>}
                <div className="flex flex-wrap gap-2">
                  <Button type="submit" variant="primary" loading={busy} disabled={busy || !valid.length || !departmentId}><Send /> Invite {Math.min(valid.length, MAX) || ""} {valid.length === 1 ? "employee" : "employees"}</Button>
                  {results.some((row) => row.link) && <Button onClick={downloadLinks}><Download /> Download {results.filter((row) => row.link).length} invitation links</Button>}
                </div>
                {results.some((row) => row.error) && <ul className="grid gap-1 text-xs text-danger-text">{results.filter((row) => row.error).map((row) => <li key={row.email}><strong>{row.email}</strong>: {row.error}</li>)}</ul>}
              </form>
            </CardBody>
          </Card>
          <Card>
            <CardHeader title="Waiting to sign up" description={departmentName ? `Invitations you sent to ${departmentName}` : undefined} />
            <CardBody>
              {pending.length === 0 ? <p className="text-xs text-muted">No pending invitations.</p> : (
                <ul className="grid gap-2">{pending.map((row) => <li key={row.id} className="flex items-center justify-between gap-2 text-[13px]"><span className="truncate">{row.email}</span><Badge tone="warning">Pending</Badge></li>)}</ul>
              )}
            </CardBody>
          </Card>
        </div>
      )}
    </>
  );
}
