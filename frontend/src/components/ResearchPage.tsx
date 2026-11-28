import { useEffect, useState } from "react";
import { api, describeError } from "../api";
import { citationPlace, mediaPreviewHref } from "../format";
import type { LegacyBrowserCapture, LegacyBrowserRun, LegacyBrowserStatus, ResearchReport, ResearchSpecialist, User } from "../types";
import { Markdown } from "./Markdown";
import { useFeedback } from "./Feedback";
import { FileSearch, Globe, Printer, Search, Send, Telescope } from "lucide-react";
import { Alert, Badge, Button, Card, CardBody, CardHeader, EmptyState, Field, Input, PageHeader, Skeleton, Textarea } from "./ui";

const SPECIALIST_LABELS: Record<ResearchSpecialist, string> = {
  evidence_finder: "Evidence finder",
  policy_analyst: "Policy analyst",
  exception_checker: "Exception checker",
};

export function ResearchPage({ user }: { user: User }) {
  const { toast } = useFeedback();
  const [question, setQuestion] = useState("");
  const [report, setReport] = useState<ResearchReport | null>(null);
  const [busy, setBusy] = useState(false);
  const [browserStatus, setBrowserStatus] = useState<LegacyBrowserStatus | null>(null);
  const [browserRuns, setBrowserRuns] = useState<LegacyBrowserRun[]>([]);
  const [browserLabel, setBrowserLabel] = useState("");
  const [browserReason, setBrowserReason] = useState("");
  const [browserUrl, setBrowserUrl] = useState("");
  const [browserLinks, setBrowserLinks] = useState("");
  const [browserBusy, setBrowserBusy] = useState(false);
  const [capture, setCapture] = useState<LegacyBrowserCapture | null>(null);

  async function refreshBrowserRuns() {
    try {
      const [status, runs] = await Promise.all([api.legacyBrowserStatus(), api.legacyBrowserRuns()]);
      setBrowserStatus(status);
      setBrowserRuns(runs.items);
    } catch (err) {
      toast("error", "Browser review status could not load", describeError(err));
    }
  }

  useEffect(() => { void refreshBrowserRuns(); }, []);

  async function requestBrowserRun(event: React.FormEvent) {
    event.preventDefault();
    if (browserBusy) return;
    setBrowserBusy(true);
    try {
      const created = await api.requestLegacyBrowserRun({
        label: browserLabel.trim(),
        reason: browserReason.trim(),
        start_url: browserUrl.trim(),
        link_labels: browserLinks.split("\n").map((item) => item.trim()).filter(Boolean),
      });
      setBrowserLabel(""); setBrowserReason(""); setBrowserUrl(""); setBrowserLinks("");
      await refreshBrowserRuns();
      toast("success", "Review requested", `Request ${created.id.slice(0, 8)} is waiting for an independent Admin.`);
    } catch (err) { toast("error", "Browser review was not requested", describeError(err)); }
    finally { setBrowserBusy(false); }
  }

  async function approveBrowserRun(runId: string) {
    setBrowserBusy(true);
    try {
      const result = await api.approveLegacyBrowserRun(runId);
      setCapture(result);
      await refreshBrowserRuns();
      toast(result.run.status === "completed" ? "success" : "error", result.run.status === "completed" ? "Read-only review complete" : "Read-only review failed", result.run.failure_code ?? `${result.pages.length} page(s) captured.`);
    } catch (err) { toast("error", "Browser review could not run", describeError(err)); }
    finally { setBrowserBusy(false); }
  }

  async function changeBrowserRun(runId: string, action: "reject" | "cancel") {
    setBrowserBusy(true);
    try {
      if (action === "reject") await api.rejectLegacyBrowserRun(runId);
      else await api.cancelLegacyBrowserRun(runId);
      setCapture(null);
      await refreshBrowserRuns();
    } catch (err) { toast("error", "Browser request was not changed", describeError(err)); }
    finally { setBrowserBusy(false); }
  }

  async function run(event: React.FormEvent) {
    event.preventDefault();
    if (!question.trim() || busy) return;
    setBusy(true);
    setReport(null);
    try { setReport(await api.researchDocuments(question.trim())); }
    catch (err) { toast("error", "Research could not run", describeError(err)); }
    finally { setBusy(false); }
  }

  const supported = report?.findings.filter((finding) => !finding.insufficient_evidence).length ?? 0;

  return (
    <>
      <PageHeader
        title="Company data research"
        description="Ask a deeper question across internal reports, policies and tables. Up to three specialists search only documents you can read, and every finding is access-checked and citation-validated."
      />

      <Card className="research-form mb-5 overflow-hidden">
        <CardHeader icon={<Telescope />} title="Research question" description="Answers come only from documents. If the library does not support a finding, the report says so." />
        <CardBody>
          <form className="grid gap-3" onSubmit={(event) => void run(event)}>
            <Textarea aria-label="Research question" value={question} onChange={(event) => setQuestion(event.target.value)} maxLength={4000} rows={4} placeholder="Compare the main causes of service delays across the latest incident reports, and identify any open evidence gaps." />
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex flex-wrap gap-1.5">{(Object.keys(SPECIALIST_LABELS) as ResearchSpecialist[]).map((key) => <Badge key={key}>{SPECIALIST_LABELS[key]}</Badge>)}</div>
              <Button type="submit" variant="primary" loading={busy} disabled={busy || !question.trim()}>{!busy && <Search />} Run research</Button>
            </div>
          </form>
        </CardBody>
      </Card>

      {busy && (
        <Card className="mb-5 grid gap-3 p-5" aria-live="polite">
          <p className="text-[13px] text-foreground-2">Assigning read-only specialists, searching authorized documents and checking citations…</p>
          <Skeleton className="h-24" /><Skeleton className="h-24" />
        </Card>
      )}

      {report && (
        <section className="research-results mb-5 grid gap-4">
          <div className="flex flex-wrap items-end justify-between gap-3">
            <div>
              <h2 className="text-base font-semibold text-foreground">Research findings</h2>
              <p className="text-xs text-muted">{report.findings.length} evidence question{report.findings.length === 1 ? "" : "s"} · {supported} supported · {report.provider ?? "configured provider"} {report.model ?? ""}</p>
            </div>
            <Button size="sm" variant="ghost" onClick={() => window.print()}><Printer /> Print report</Button>
          </div>
          {report.findings.map((finding, index) => (
            <article className="research-finding rounded-xl border border-border bg-surface p-5 shadow-xs" key={`${index}-${finding.question}`}>
              <div className="mb-2 flex flex-wrap items-center gap-1.5">
                <Badge tone="primary">Question {index + 1}</Badge>
                {finding.specialist && <Badge>{SPECIALIST_LABELS[finding.specialist]}</Badge>}
                <Badge tone={finding.insufficient_evidence ? "warning" : "success"} dot>{finding.insufficient_evidence ? "Evidence gap" : "Supported"}</Badge>
              </div>
              <h3 className="mb-2 text-sm font-semibold text-foreground">{finding.question}</h3>
              <div className="research-answer text-[13px]"><Markdown text={finding.answer} renderCitation={(tag, key) => <span className="source-tag" key={key}>{tag}</span>} /></div>
              {finding.citations.length > 0 && (
                <div className="research-sources mt-3">
                  <strong>Sources</strong>
                  {finding.citations.map((citation) => (
                    <div key={`${citation.document_id}-${citation.chunk_index}`} className="research-source">
                      <span className="source-tag">{citation.source_id}</span>
                      <span><strong>{citation.filename}</strong><small className="text-xs">{citation.location ?? `Page ${citation.page_number}`}</small><small className="text-xs">{citation.snippet}</small>{citation.available && mediaPreviewHref(citation) && <a className="source-page-link" href={mediaPreviewHref(citation) ?? undefined} target="_blank" rel="noopener noreferrer">Open original media at {citationPlace(citation)}</a>}</span>
                    </div>
                  ))}
                </div>
              )}
            </article>
          ))}
          <p className="text-xs text-muted">Every specialist is limited to read-only knowledge search; department access is applied separately for each finding.</p>
        </section>
      )}

      {!report && !busy && <Card className="mb-5"><EmptyState icon={<FileSearch />} title="No research yet" description="Ask a question above. Findings appear here with their sources." /></Card>}

      <Card className="research-browser overflow-hidden" aria-labelledby="legacy-browser-title">
        <CardHeader
          icon={<Globe />}
          title={<span id="legacy-browser-title">Controlled legacy-site review</span>}
          description="Request a small read-only capture from a site on the operator’s allowlist. Another administrator must approve every run."
          actions={<Badge tone={browserStatus?.enabled ? "success" : "warning"} dot>{browserStatus?.enabled ? "Available" : "Not configured"}</Badge>}
        />
        <CardBody className="grid gap-5">
          {browserStatus?.enabled && (
            <form className="grid gap-3" onSubmit={(event) => void requestBrowserRun(event)}>
              <div className="grid gap-3 md:grid-cols-2">
                <Field label="Review label"><Input value={browserLabel} onChange={(event) => setBrowserLabel(event.target.value)} required minLength={3} maxLength={120} /></Field>
                <Field label="Allowlisted start URL"><Input type="url" value={browserUrl} onChange={(event) => setBrowserUrl(event.target.value)} required maxLength={2048} placeholder="https://legacy.example.org/overview" /></Field>
              </div>
              <Field label="Why is this site needed?"><Textarea value={browserReason} onChange={(event) => setBrowserReason(event.target.value)} required minLength={10} maxLength={1000} rows={2} /></Field>
              <Field label="Exact link labels to open (up to 3, one per line)"><Textarea value={browserLinks} onChange={(event) => setBrowserLinks(event.target.value)} rows={3} placeholder={"Policies\nArchive"} /></Field>
              <Alert tone="info">
                Runs use a fresh browser with no cookies and only GET requests to exactly allowlisted sites: at most 4 pages, strict time and size limits, no downloads or form submissions. Some sites change data through GET, so approve only known-safe pages. Captured text is shown to the approver, is not saved, and cannot trigger actions or AI requests.
              </Alert>
              <div><Button type="submit" variant="primary" loading={browserBusy} disabled={browserBusy || !browserLabel.trim() || !browserReason.trim() || !browserUrl.trim()}>{!browserBusy && <Send />} Request independent approval</Button></div>
            </form>
          )}
          {browserStatus && !browserStatus.enabled && <p className="text-xs text-muted">An administrator can enable this with an exact site allowlist, an approved browser executable and the optional browser dependency.</p>}

          <div className="grid gap-2" aria-live="polite">
            <h3 className="text-[13px] font-semibold text-foreground">{user.role === "admin" ? "Requests awaiting review and recent runs" : "My browser requests"}</h3>
            {browserRuns.length === 0 && <p className="text-xs text-muted">No browser requests yet.</p>}
            {browserRuns.map((run) => (
              <article className="research-browser-run grid gap-2 rounded-lg border border-border p-4" key={run.id}>
                <div className="flex flex-wrap items-center gap-1.5">
                  <Badge tone={run.status === "completed" ? "success" : run.status === "failed" ? "danger" : run.status === "pending_approval" ? "warning" : "neutral"} dot>{run.status.replaceAll("_", " ")}</Badge>
                  <Badge>{run.attempts}/2 attempts</Badge>
                  <code className="text-xs text-muted">{run.id.slice(0, 8)}</code>
                </div>
                <h4 className="text-[13px] font-semibold text-foreground">{run.label}</h4>
                <p className="text-[13px] text-foreground-2">{run.reason}</p>
                <p className="text-xs text-muted [overflow-wrap:anywhere]"><code>{run.start_url}</code>{run.link_labels.length > 0 && <> · Links: {run.link_labels.join(", ")}</>}</p>
                {run.failure_code && <p className="text-xs text-danger-text">Failure code: <code>{run.failure_code}</code></p>}
                <div className="flex flex-wrap gap-2">
                  {user.role === "admin" && (run.status === "pending_approval" || run.status === "failed") && run.attempts < 2 && <>
                    <Button size="sm" variant="primary" disabled={browserBusy || run.requester_user_id === user.id} onClick={() => void approveBrowserRun(run.id)}>{browserBusy ? "Reviewing…" : run.status === "failed" ? "Approve one retry" : "Approve and capture"}</Button>
                    <Button size="sm" disabled={browserBusy || run.status !== "pending_approval" || run.requester_user_id === user.id} onClick={() => void changeBrowserRun(run.id, "reject")}>Reject</Button>
                  </>}
                  {run.requester_user_id === user.id && run.status === "pending_approval" && <Button size="sm" variant="ghost" disabled={browserBusy} onClick={() => void changeBrowserRun(run.id, "cancel")}>Cancel request</Button>}
                </div>
              </article>
            ))}
          </div>

          {capture && (
            <section className="research-browser-capture grid gap-3" aria-live="polite">
              <div>
                <h3 className="text-[13px] font-semibold text-foreground">Captured read-only pages · not saved</h3>
                <p className="text-xs text-muted">{capture.request_count} bounded requests · {capture.response_bytes.toLocaleString()} response bytes. The text below is untrusted external content.</p>
              </div>
              {capture.pages.map((page, index) => (
                <article className="card grid gap-2 p-4" key={`${index}-${page.url}`}>
                  <h4 className="text-[13px] font-semibold">{page.label}: {page.title || "Untitled page"}</h4>
                  <code className="text-xs text-muted [overflow-wrap:anywhere]">{page.url}</code>
                  <pre className="research-browser-text max-h-80 overflow-auto rounded-md bg-surface-2 p-3 text-xs whitespace-pre-wrap">{page.text}</pre>
                </article>
              ))}
            </section>
          )}
        </CardBody>
      </Card>
    </>
  );
}
