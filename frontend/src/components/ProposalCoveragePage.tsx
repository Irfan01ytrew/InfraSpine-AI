import { useEffect, useState } from "react";
import { api, describeError } from "../api";
import { citationPlace, mediaPreviewHref, formatDate } from "../format";
import type {
  ProposalAssessment,
  ProposalCoverageCase,
  ProposalCoverageOptions,
  ProposalCoverageSummary,
  ProposalRequirementCandidate,
  RagDocument,
} from "../types";
import { Markdown } from "./Markdown";
import { useFeedback } from "./Feedback";
import { Spinner } from "../icons";
import { Alert, PageHeader, SelectInput } from "./ui";

export function ProposalCoveragePage() {
  const { toast } = useFeedback();
  const [options, setOptions] = useState<ProposalCoverageOptions | null>(null);
  const [cases, setCases] = useState<ProposalCoverageSummary[]>([]);
  const [documents, setDocuments] = useState<RagDocument[]>([]);
  const [selected, setSelected] = useState<ProposalCoverageCase | null>(null);
  const [title, setTitle] = useState("");
  const [departmentId, setDepartmentId] = useState("");
  const [requirementsText, setRequirementsText] = useState("");
  const [newRequirementSourceIds, setNewRequirementSourceIds] = useState<Record<string, number[]>>({});
  const [sourceDocumentIds, setSourceDocumentIds] = useState<string[]>([]);
  const [extractedCandidates, setExtractedCandidates] = useState<ProposalRequirementCandidate[]>([]);
  const [selectedExtractedCandidates, setSelectedExtractedCandidates] = useState<number[]>([]);
  const [extractionComplete, setExtractionComplete] = useState(false);
  const [documentLoadError, setDocumentLoadError] = useState<string | null>(null);
  const [editTitle, setEditTitle] = useState("");
  const [editDepartmentId, setEditDepartmentId] = useState("");
  const [editRequirements, setEditRequirements] = useState("");
  const [editRequirementSourceIds, setEditRequirementSourceIds] = useState<Record<string, number[]>>({});
  const [assessmentValues, setAssessmentValues] = useState<Record<string, ProposalAssessment>>({});
  const [assessmentNotes, setAssessmentNotes] = useState<Record<string, string>>({});
  const [reviewNote, setReviewNote] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [exportingFormat, setExportingFormat] = useState<"pdf" | "docx" | "xlsx" | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function refresh(selectedId?: string) {
    const [nextOptions, nextCases] = await Promise.all([api.proposalCoverageOptions(), api.proposalCoverageCases()]);
    setOptions(nextOptions);
    setCases(nextCases.cases);
    const id = selectedId ?? selected?.id;
    if (id) {
      const row = await api.proposalCoverageCase(id);
      setSelected(row);
      syncEditor(row);
    }
  }

  function syncEditor(row: ProposalCoverageCase) {
    setEditTitle(row.title);
    setEditDepartmentId(row.department_group_id ?? "");
    setEditRequirements(row.requirements.map((item) => item.requirement).join("\n"));
    setEditRequirementSourceIds(Object.fromEntries(row.requirements
      .filter((item) => item.origin_evidence_current && item.origin_evidence.length > 0)
      .map((item) => [item.requirement, item.origin_evidence.map((source) => source.chunk_id)])));
  }

  useEffect(() => {
    let active = true;
    void api.documents()
      .then((result) => { if (active) setDocuments(result.documents); })
      .catch((err) => { if (active) setDocumentLoadError(describeError(err)); });
    void Promise.all([api.proposalCoverageOptions(), api.proposalCoverageCases()])
      .then(([nextOptions, nextCases]) => {
        if (!active) return;
        setOptions(nextOptions);
        setCases(nextCases.cases);
      })
      .catch((err) => { if (active) setError(describeError(err)); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, []);

  function parseRequirements(value: string, sourceIdsByRequirement: Record<string, number[]>) {
    return value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean).map((requirement) => ({
      requirement,
      source_chunk_ids: sourceIdsByRequirement[requirement] ?? [],
    }));
  }

  function keepSourceIdsForRemainingRequirements(value: string, current: Record<string, number[]>) {
    const remaining = new Set(value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean));
    return Object.fromEntries(Object.entries(current).filter(([requirement]) => remaining.has(requirement)));
  }

  const rfpDocuments = documents.filter((document) =>
    document.status === "ready" && document.indexed_version === document.version &&
    (document.file_format === "pdf" || document.file_format === "docx" || document.file_format === "xlsx"),
  );

  async function extractRfpRequirements() {
    if (busy || sourceDocumentIds.length === 0 || sourceDocumentIds.length > 5) return;
    setBusy(true);
    setError(null);
    setExtractionComplete(false);
    try {
      const result = await api.extractProposalRequirements(sourceDocumentIds);
      setExtractedCandidates(result.candidates);
      setSelectedExtractedCandidates(result.candidates.map((_candidate, index) => index));
      setExtractionComplete(true);
      if (result.candidates.length > 0) toast("success", "Requirement candidates are ready for review");
    } catch (err) {
      setError(describeError(err));
    } finally { setBusy(false); }
  }

  function useSelectedExtractedCandidates() {
    const additions = extractedCandidates
      .filter((_candidate, index) => selectedExtractedCandidates.includes(index));
    if (additions.length === 0) return;
    setNewRequirementSourceIds((current) => ({
      ...current,
      ...Object.fromEntries(additions.map((candidate) => [
        candidate.requirement.trim(),
        candidate.evidence.map((source) => source.chunk_id),
      ])),
    }));
    setRequirementsText((current) => {
      const lines = parseRequirements(current, newRequirementSourceIds).map((item) => item.requirement);
      const seen = new Set(lines.map((line) => line.toLocaleLowerCase()));
      for (const { requirement } of additions) {
        if (!seen.has(requirement.toLocaleLowerCase())) {
          lines.push(requirement);
          seen.add(requirement.toLocaleLowerCase());
        }
      }
      return lines.join("\n");
    });
  }

  async function runAction(action: () => Promise<ProposalCoverageCase>, message: string) {
    if (!selected || busy) return;
    setBusy(true);
    setError(null);
    try {
      const row = await action();
      setSelected(row);
      syncEditor(row);
      setAssessmentValues({});
      setAssessmentNotes({});
      setReviewNote("");
      await refresh(row.id);
      toast("success", message);
    } catch (err) {
      setError(describeError(err));
    } finally { setBusy(false); }
  }

  async function create(event: React.FormEvent) {
    event.preventDefault();
    if (busy) return;
    const requirements = parseRequirements(requirementsText, newRequirementSourceIds);
    setBusy(true);
    setError(null);
    try {
      const row = await api.createProposalCoverageCase({
        title: title.trim(), department_group_id: departmentId || null, requirements,
      });
      setSelected(row);
      syncEditor(row);
      setTitle(""); setRequirementsText("");
      setNewRequirementSourceIds({});
      setSourceDocumentIds([]); setExtractedCandidates([]); setSelectedExtractedCandidates([]); setExtractionComplete(false);
      await refresh(row.id);
      toast("success", "Proposal coverage case created");
    } catch (err) { setError(describeError(err)); }
    finally { setBusy(false); }
  }

  async function saveDraft(event: React.FormEvent) {
    event.preventDefault();
    if (!selected || busy) return;
    const requirements = parseRequirements(editRequirements, editRequirementSourceIds);
    setBusy(true); setError(null);
    try {
      const row = await api.updateProposalCoverageCase(selected.id, {
        title: editTitle.trim(), department_group_id: editDepartmentId || null, requirements,
      });
      setSelected(row); syncEditor(row); await refresh(row.id);
      toast("success", "Proposal draft saved");
    } catch (err) { setError(describeError(err)); }
    finally { setBusy(false); }
  }

  async function analyze(requirementId: string) {
    if (!selected || busy) return;
    setBusy(true); setError(null);
    try {
      const row = await api.analyzeProposalRequirement(selected.id, requirementId);
      setSelected(row); syncEditor(row); await refresh(row.id);
      toast("success", "Evidence check completed");
    } catch (err) { setError(describeError(err)); }
    finally { setBusy(false); }
  }

  async function exportCase(format: "pdf" | "docx" | "xlsx") {
    if (!selected || exportingFormat) return;
    setExportingFormat(format);
    setError(null);
    try {
      const blob = await api.proposalCoverageExport(selected.id, format);
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `proposal-coverage-r${selected.revision_number}-${selected.id.slice(0, 8)}.${format}`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      // Revoking right after click() can cancel the download in some browsers; give it time to start.
      window.setTimeout(() => URL.revokeObjectURL(url), 30_000);
      toast("success", `Coverage report downloaded as ${format.toUpperCase()}`);
    } catch (err) {
      setError(describeError(err));
    } finally {
      setExportingFormat(null);
    }
  }

  async function assess(requirementId: string) {
    if (!selected) return;
    const assessment = assessmentValues[requirementId];
    const rationale = assessmentNotes[requirementId]?.trim() ?? "";
    if (!assessment || assessment === "not_assessed") { setError("Choose a reviewer assessment first."); return; }
    await runAction(() => api.assessProposalRequirement(selected.id, requirementId, assessment, rationale), "Requirement assessment saved");
  }

  if (loading) return <div className="loading"><Spinner size={20} /> Loading proposal coverage…</div>;

  return <>
    <PageHeader
      title={<>Tender &amp; RFP coverage</>}
      description="Map each requirement to evidence in documents you can access. The AI reports source findings and gaps; a department reviewer makes the coverage judgment."
    />
    {error && <Alert tone="danger" className="mb-4">{error}</Alert>}
    <div className="proposal-coverage-grid">
      <section className="proposal-coverage-list">
        {options?.departments.length === 0 && <Alert tone="warning" className="mb-4">Join a department before submitting a case for review. A private draft can still be saved.</Alert>}
        <section className="card">
          <div className="card-title"><div><h2>New coverage case</h2><p className="muted">Draft requirements manually or extract candidates from an uploaded RFP. Review and edit every candidate before creating the case.</p></div></div>
          <form onSubmit={(event) => void create(event)} className="proposal-form">
            <label className="field"><span className="field-label">Tender or proposal title</span><input value={title} onChange={(event) => setTitle(event.target.value)} maxLength={180} required /></label>
            <label className="field"><span className="field-label">Review department</span><SelectInput value={departmentId} onChange={(event) => setDepartmentId(event.target.value)}><option value="">Private draft (no review)</option>{options?.departments.map((group) => <option key={group.id} value={group.id}>{group.name}</option>)}</SelectInput></label>
            <section className="proposal-extraction-panel" aria-label="Extract requirements from an uploaded RFP">
              <label className="field"><span className="field-label">Indexed RFP source files</span>
                <select multiple size={Math.min(5, Math.max(2, rfpDocuments.length))} value={sourceDocumentIds} onChange={(event) => {
                  const next = Array.from(event.currentTarget.selectedOptions, (option) => option.value);
                  setSourceDocumentIds(next);
                }} aria-label="Select up to five PDF, DOCX, or XLSX RFP files">
                  {rfpDocuments.map((document) => <option key={document.id} value={document.id}>{document.filename} · {document.file_format.toUpperCase()}</option>)}
                </select>
                <small className="muted text-xs">Select up to five accessible, indexed PDF, DOCX, or XLSX files. Upload an RFP in Knowledge first if it is not listed.</small>
              </label>
              {documentLoadError && <Alert tone="warning" className="mb-4">Uploaded source list is unavailable: {documentLoadError}</Alert>}
              <button type="button" className="btn" disabled={busy || sourceDocumentIds.length === 0 || sourceDocumentIds.length > 5} onClick={() => void extractRfpRequirements()}>
                {busy ? <Spinner size={14} /> : null} Draft requirement candidates
              </button>
              {sourceDocumentIds.length > 5 && <p className="field-error">Select no more than five RFP files.</p>}
              {extractionComplete && extractedCandidates.length === 0 && <p className="muted">No explicit requirements were found in the selected passages. You can still enter requirements manually.</p>}
              {extractedCandidates.length > 0 && <div className="proposal-extraction-candidates">
                <h3>Review extracted candidates</h3>
                <p className="muted">These are unapproved drafts. Check the RFP evidence, edit the wording, and choose which candidates to add.</p>
                {extractedCandidates.map((candidate, index) => <article className="proposal-extraction-candidate" key={`${index}-${candidate.evidence[0]?.chunk_id}`}>
                  <label><input type="checkbox" checked={selectedExtractedCandidates.includes(index)} onChange={(event) => setSelectedExtractedCandidates((current) => event.target.checked ? [...current, index] : current.filter((value) => value !== index))} /> Include candidate {index + 1}</label>
                  <textarea aria-label={`Extracted requirement candidate ${index + 1}`} value={candidate.requirement} maxLength={2000} rows={2} onChange={(event) => setExtractedCandidates((current) => current.map((value, candidateIndex) => candidateIndex === index ? { ...value, requirement: event.target.value } : value))} />
                  {candidate.evidence.map((evidence) => <div className="proposal-extraction-evidence" key={`${evidence.document_id}-${evidence.chunk_id}`}>
                    <strong>{evidence.filename}</strong><small className="text-xs">{evidence.location}</small><small className="text-xs">{evidence.excerpt}</small>
                  </div>)}
                </article>)}
                <button type="button" className="btn" disabled={selectedExtractedCandidates.length === 0} onClick={useSelectedExtractedCandidates}>Add selected candidates to requirements</button>
              </div>}
            </section>
            <label className="field"><span className="field-label">Requirements</span><textarea value={requirementsText} onChange={(event) => {
              const value = event.target.value;
              setRequirementsText(value);
              setNewRequirementSourceIds((current) => keepSourceIdsForRemainingRequirements(value, current));
            }} rows={6} maxLength={60_000} placeholder={"Example: Provide 24/7 support with a documented escalation process.\nExample: Retain operational records for at least five years."} required /></label>
            <button className="btn btn-primary" disabled={busy || !title.trim() || parseRequirements(requirementsText, newRequirementSourceIds).length === 0 || parseRequirements(requirementsText, newRequirementSourceIds).length > 30}>{busy && <Spinner size={14} />} Create case</button>
          </form>
        </section>
        <section className="card">
          <div className="card-title"><div><h2>Cases</h2><p className="muted">Your drafts and proposals assigned for scoped review.</p></div><button className="btn" onClick={() => void refresh().catch((err) => setError(describeError(err)))}>Refresh</button></div>
          {cases.length === 0 ? <p className="empty">No proposal cases yet.</p> : <div className="proposal-case-list">{cases.map((row) => <button key={row.id} className={`proposal-case-row ${selected?.id === row.id ? "proposal-case-selected" : ""}`} onClick={() => void api.proposalCoverageCase(row.id).then((detail) => { setSelected(detail); syncEditor(detail); setError(null); }).catch((err) => setError(describeError(err)))}><span><strong>{row.title}</strong><small className="text-xs">{row.department_name ?? "Private"} · {row.requirement_count} requirements · {row.analyzed_count} evidence checks</small></span><span className={`pill pill-sm ${row.status === "approved" ? "pill-success" : row.status === "changes_requested" ? "pill-warning" : ""}`}>{row.status.replaceAll("_", " ")}</span></button>)}</div>}
        </section>
      </section>
      <section className="proposal-coverage-detail">
        {!selected ? <div className="card empty">Select a case to review its evidence map.</div> : <>
          <section className="card">
            <div className="proposal-case-heading"><div><span className="chip chip-sm">Revision {selected.revision_number || "draft"}</span><h2>{selected.title}</h2><p className="muted">{selected.department_name ?? "Private draft"} · {selected.owner_email} · {selected.status.replaceAll("_", " ")}</p></div><button className="btn" onClick={() => void refresh(selected.id).catch((err) => setError(describeError(err)))}>Refresh case</button></div>
            <div className="proposal-case-actions" aria-label="Download proposal coverage report">
              {(["pdf", "docx", "xlsx"] as const).map((format) => <button key={format} className="btn" disabled={Boolean(exportingFormat)} onClick={() => void exportCase(format)}>
                {exportingFormat === format ? <Spinner size={14} /> : null} Export {format === "docx" ? "Word" : format === "xlsx" ? "Excel" : "PDF"}
              </button>)}
            </div>
            {selected.can_edit && <form onSubmit={(event) => void saveDraft(event)} className="proposal-form proposal-edit-form">
              <label className="field"><span className="field-label">Title</span><input value={editTitle} onChange={(event) => setEditTitle(event.target.value)} maxLength={180} required /></label>
              <label className="field"><span className="field-label">Department</span><SelectInput value={editDepartmentId} onChange={(event) => setEditDepartmentId(event.target.value)}><option value="">Private draft</option>{options?.departments.map((group) => <option key={group.id} value={group.id}>{group.name}</option>)}</SelectInput></label>
              <label className="field"><span className="field-label">Requirements · one per line</span><textarea value={editRequirements} onChange={(event) => {
                const value = event.target.value;
                setEditRequirements(value);
                setEditRequirementSourceIds((current) => keepSourceIdsForRemainingRequirements(value, current));
              }} rows={5} maxLength={60_000} required /></label>
              <button className="btn" disabled={busy || !editTitle.trim() || parseRequirements(editRequirements, editRequirementSourceIds).length === 0 || parseRequirements(editRequirements, editRequirementSourceIds).length > 30}>Save draft</button>
            </form>}
            <div className="proposal-case-actions">
              {selected.can_submit && <button className="btn btn-primary" disabled={busy} onClick={() => void runAction(() => api.submitProposalCoverageCase(selected.id), "Submitted for department review")}>Submit for review</button>}
              {selected.can_edit && <button className="btn" disabled={busy} onClick={() => void runAction(() => api.withdrawProposalCoverageCase(selected.id), "Case withdrawn")}>Withdraw</button>}
              <span className="muted small">Evidence checks: {selected.analyzed_count}/{selected.requirement_count} · reviewed assessments: {selected.assessment_count}/{selected.requirement_count}</span>
            </div>
          </section>

          {selected.requirements.map((item) => <article className="card proposal-requirement" key={item.id}>
            <div className="proposal-requirement-top"><span className="chip chip-sm">Requirement {item.position}</span><span className={`pill pill-sm ${item.assessment === "meets" ? "pill-success" : item.assessment === "gap" || item.insufficient_evidence ? "pill-warning" : ""}`}>{item.assessment === "not_assessed" ? (item.analyzed_at ? item.insufficient_evidence ? "Evidence gap · review needed" : "Evidence found · review needed" : "Not checked") : item.assessment}</span></div>
            <h3>{item.requirement}</h3>
            {!item.origin_evidence_current && <Alert tone="warning" className="mb-4">The original RFP passage was hidden because its source changed or access was revoked.</Alert>}
            {item.origin_evidence.length > 0 && <div className="proposal-origin-evidence"><strong>Original RFP passages</strong>{item.origin_evidence.map((source) => <div className="proposal-extraction-evidence" key={`${source.document_id}-${source.chunk_id}`}><strong>{source.filename}</strong><small className="text-xs">{source.location}</small><small className="text-xs">{source.excerpt}</small></div>)}</div>}
            {item.analyzed_at ? <>
              {!item.evidence_current && <Alert tone="warning" className="mb-4">The saved finding was hidden because a source changed or access was revoked. Run the evidence check again.</Alert>}
              <div className="research-answer"><Markdown text={item.answer} renderCitation={(tag, key) => <span className="source-tag" key={key}>{tag}</span>} /></div>
              {item.citations.length > 0 && <div className="research-sources"><strong>Source passages</strong>{item.citations.map((citation) => <div className="research-source" key={`${citation.document_id}-${citation.chunk_index}`}><span className="source-tag">{citation.source_id}</span><span><strong>{citation.filename}</strong><small className="text-xs">{citation.location ?? `Page ${citation.page_number}`}</small><small className="text-xs">{citation.snippet}</small>{mediaPreviewHref(citation) && <a className="source-page-link" href={mediaPreviewHref(citation) ?? undefined} target="_blank" rel="noopener noreferrer">Open source at {citationPlace(citation)}</a>}</span></div>)}</div>}
              {item.assessment !== "not_assessed" && <p className="proposal-review-note"><strong>Reviewer:</strong> {item.assessment.replaceAll("_", " ")} · {item.assessment_rationale}</p>}
            </> : <p className="muted">No evidence check has been run yet.</p>}
            <div className="proposal-case-actions">
              {selected.can_edit && <button className="btn" disabled={busy} onClick={() => void analyze(item.id)}>{busy ? <Spinner size={14} /> : null} {item.analyzed_at ? "Refresh evidence" : "Find evidence"}</button>}
              {selected.can_review && <div className="proposal-assessment-form"><SelectInput aria-label={`Assessment for requirement ${item.position}`} value={assessmentValues[item.id] ?? (item.assessment === "not_assessed" ? "" : item.assessment)} onChange={(event) => setAssessmentValues((current) => ({ ...current, [item.id]: event.target.value as ProposalAssessment }))}><option value="">Choose human assessment</option><option value="meets">Meets</option><option value="partial">Partially meets</option><option value="gap">Evidence gap</option></SelectInput><input aria-label={`Rationale for requirement ${item.position}`} value={assessmentNotes[item.id] ?? item.assessment_rationale} onChange={(event) => setAssessmentNotes((current) => ({ ...current, [item.id]: event.target.value }))} placeholder="Reviewer rationale" maxLength={2000} /><button className="btn" disabled={busy} onClick={() => void assess(item.id)}>Save assessment</button></div>}
            </div>
          </article>)}

          {selected.can_review && <section className="card proposal-review-panel"><h3>Department review</h3><p className="muted">Approval records a human decision and the reviewed coverage snapshot. Every requirement needs an assessment first.</p><label className="field"><span className="field-label">Review rationale</span><textarea value={reviewNote} onChange={(event) => setReviewNote(event.target.value)} rows={3} maxLength={2000} /></label><div className="proposal-case-actions"><button className="btn btn-primary" disabled={busy || selected.assessment_count !== selected.requirement_count || !reviewNote.trim()} onClick={() => void runAction(() => api.reviewProposalCoverageCase(selected.id, "approved", reviewNote), "Proposal coverage approved")}>Approve coverage</button><button className="btn" disabled={busy || !reviewNote.trim()} onClick={() => void runAction(() => api.reviewProposalCoverageCase(selected.id, "changes_requested", reviewNote), "Changes requested")}>Request changes</button></div></section>}

          {selected.reviews.length > 0 && <section className="card proposal-review-panel"><h3>Review history</h3>{selected.reviews.map((review) => <div className="proposal-history-row" key={review.id}><span className={`pill pill-sm ${review.decision === "approved" ? "pill-success" : "pill-warning"}`}>{review.decision.replaceAll("_", " ")}</span><span><strong>{review.reviewer_email}</strong><small className="text-xs">Revision {review.revision_number} · {formatDate(review.reviewed_at)}</small><small className="text-xs">{review.rationale}</small></span></div>)}</section>}
        </>}
      </section>
    </div>
  </>;
}
