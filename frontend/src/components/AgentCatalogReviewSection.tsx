import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, describeError } from "../api";
import { formatDay } from "../format";
import type {
  AgentCatalogDefinition,
  AgentCatalogGrant,
  AgentCatalogManagedTemplate,
  AgentCatalogTemplate,
  AgentKnowledgeScope,
  AgentTool,
  AgentType,
  User,
} from "../types";
import { SelectInput } from "./ui";
import { useLiveRefresh } from "../lib/useLiveRefresh";

const AGENT_TYPES: Array<{ id: AgentType; label: string }> = [
  { id: "knowledge_research", label: "Knowledge research" },
  { id: "market_research", label: "Market research" },
  { id: "company_analysis", label: "Company data analysis" },
  { id: "employee_training", label: "Employee training" },
];
const TOOL_OPTIONS: Record<AgentType, Array<{ id: AgentTool; label: string }>> = {
  knowledge_research: [{ id: "knowledge_search", label: "Search company knowledge" }],
  market_research: [
    { id: "knowledge_search", label: "Search company knowledge" },
    { id: "public_web_research", label: "Public web research (external consent required)" },
  ],
  company_analysis: [
    { id: "knowledge_search", label: "Search company knowledge" },
    { id: "company_data_analysis", label: "Company data analysis" },
  ],
  employee_training: [
    { id: "knowledge_search", label: "Search company knowledge" },
    { id: "training_plan", label: "Employee training plans" },
  ],
};

const EMPTY: AgentCatalogDefinition = {
  name: "",
  summary: "",
  agent_type: "knowledge_research",
  knowledge_scope: "requester_acl",
  tool_policy: ["knowledge_search"],
};

export function AgentCatalogReviewSection({
  user,
  onUseTemplate,
}: {
  user: User;
  onUseTemplate: (template: AgentCatalogTemplate) => void;
}) {
  const [published, setPublished] = useState<AgentCatalogTemplate[]>([]);
  const [managed, setManaged] = useState<AgentCatalogManagedTemplate[]>([]);
  const [grants, setGrants] = useState<AgentCatalogGrant[]>([]);
  const [canPublish, setCanPublish] = useState(false);
  const [canReview, setCanReview] = useState(false);
  const [definition, setDefinition] = useState(EMPTY);
  const [slug, setSlug] = useState("");
  const [versionOf, setVersionOf] = useState<string | null>(null);
  const [grantEmail, setGrantEmail] = useState("");
  const [grantPermission, setGrantPermission] = useState<"publish" | "review">("publish");
  const [reviewRationales, setReviewRationales] = useState<Record<string, string>>({});
  const [revokeRationales, setRevokeRationales] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const catalog = await api.agentCatalog();
      setPublished(catalog.items);
      setCanPublish(catalog.can_publish);
      setCanReview(catalog.can_review);
      if (catalog.can_publish || catalog.can_review) {
        const result = await api.managedAgentCatalog();
        setManaged(result.items);
      } else {
        setManaged([]);
      }
      if (user.role === "admin") {
        const result = await api.agentCatalogGrants();
        setGrants(result.items);
      }
      setError(null);
    } catch (err) {
      setError(describeError(err));
    }
  }, [user.role]);

  useEffect(() => { void refresh(); }, [refresh]);
  useLiveRefresh(refresh);

  function resetDraft() {
    setDefinition(EMPTY);
    setSlug("");
    setVersionOf(null);
  }

  function changeDefinition<K extends keyof AgentCatalogDefinition>(key: K, value: AgentCatalogDefinition[K]) {
    setDefinition((current) => ({ ...current, [key]: value }));
  }

  function startVersion(template: AgentCatalogManagedTemplate) {
    const source = template.versions[0];
    if (!source || template.revoked_at) return;
    setVersionOf(template.id);
    setDefinition({
      name: source.name,
      summary: source.summary,
      agent_type: source.agent_type,
      knowledge_scope: source.knowledge_scope,
      tool_policy: [...source.tool_policy],
    });
    setNotice(`Drafting version ${template.latest_version + 1} of ${template.slug}.`);
  }

  async function saveDraft(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy("draft");
    setError(null);
    setNotice(null);
    try {
      const result = versionOf
        ? await api.versionAgentCatalogTemplate(versionOf, definition)
        : await api.createAgentCatalogTemplate({ ...definition, slug });
      setNotice(`Draft version ${result.version} saved. It is not available to employees until an independent reviewer approves it.`);
      resetDraft();
      await refresh();
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(null);
    }
  }

  async function submitVersion(versionId: string) {
    setBusy(versionId);
    setError(null);
    try {
      await api.submitAgentCatalogVersion(versionId);
      setNotice("Version submitted to the independent review queue.");
      await refresh();
    } catch (err) { setError(describeError(err)); }
    finally { setBusy(null); }
  }

  async function reviewVersion(versionId: string, decision: "approve" | "reject") {
    const rationale = reviewRationales[versionId]?.trim() ?? "";
    if (rationale.length < 3) {
      setError("Enter a review rationale of at least 3 characters.");
      return;
    }
    setBusy(versionId);
    setError(null);
    try {
      await api.reviewAgentCatalogVersion(versionId, decision, rationale);
      setNotice(decision === "approve" ? "The reviewed version is now available in the internal catalog." : "The version was rejected; its history remains available to reviewers.");
      setReviewRationales((current) => ({ ...current, [versionId]: "" }));
      await refresh();
    } catch (err) { setError(describeError(err)); }
    finally { setBusy(null); }
  }

  async function revokeTemplate(templateId: string) {
    const rationale = revokeRationales[templateId]?.trim() ?? "";
    if (rationale.length < 3) {
      setError("Enter a revocation rationale of at least 3 characters.");
      return;
    }
    setBusy(templateId);
    setError(null);
    try {
      await api.revokeAgentCatalogTemplate(templateId, rationale);
      setNotice("The template was revoked and removed from the employee catalog. Review history is retained.");
      await refresh();
    } catch (err) { setError(describeError(err)); }
    finally { setBusy(null); }
  }

  async function createGrant(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy("grant");
    setError(null);
    try {
      await api.createAgentCatalogGrant(grantEmail, grantPermission);
      setGrantEmail("");
      setNotice(`${grantPermission === "publish" ? "Publisher" : "Reviewer"} grant added.`);
      await refresh();
    } catch (err) { setError(describeError(err)); }
    finally { setBusy(null); }
  }

  async function revokeGrant(grantId: string) {
    setBusy(grantId);
    setError(null);
    try {
      await api.revokeAgentCatalogGrant(grantId);
      setNotice("Catalog grant revoked; the change is retained in history.");
      await refresh();
    } catch (err) { setError(describeError(err)); }
    finally { setBusy(null); }
  }

  return <section className="card automation-card" aria-label="Reviewed internal agent catalog">
    <div className="card-title">
      <div><h2>Reviewed internal agent catalog</h2><p className="muted">Only independently approved, data-only templates appear here. Capabilities stay within the existing server allowlist.</p></div>
      <button className="btn btn-ghost" type="button" onClick={() => void refresh()}>Refresh catalog</button>
    </div>
    {error && <div className="banner banner-error" role="alert">{error}</div>}
    {notice && <div className="banner banner-success" role="status">{notice}</div>}

    {published.length === 0 ? <p className="empty">No reviewed templates are published.</p> : <div className="automation-list">{published.map((template) => <article className="automation-item" key={template.id}>
      <div className="automation-item-heading"><div><h3>{template.name}</h3><p className="muted">{template.slug} · version {template.version} · published {formatDay(template.published_at)}</p></div><span className="pill pill-success">Reviewed</span></div>
      <p>{template.summary}</p>
      <p className="small muted">{template.agent_type.replaceAll("_", " ")} · {template.knowledge_scope === "requester_acl" ? "requester’s existing document access" : "requester’s access, limited to the selected department"}</p>
      <div className="agent-tool-badges">{template.tool_policy.map((tool) => <span className="pill" key={tool}>{tool.replaceAll("_", " ")}</span>)}</div>
      {template.can_instantiate && <button className="btn btn-primary" type="button" onClick={() => onUseTemplate(template)}>Use as department profile</button>}
    </article>)}</div>}

    {canPublish && <section className="card" aria-label="Publisher template draft">
      <h3>{versionOf ? "Create a new template version" : "Create a template draft"}</h3>
      <form className="automation-form" onSubmit={(event) => void saveDraft(event)}>
        {!versionOf && <label className="field"><span className="field-label">Stable template slug</span><input required pattern="[a-z0-9]+(-[a-z0-9]+)*" maxLength={80} value={slug} onChange={(event) => setSlug(event.target.value)} placeholder="operations-research" /></label>}
        {versionOf && <p className="small muted">New version of {managed.find((template) => template.id === versionOf)?.slug}</p>}
        <label className="field"><span className="field-label">Template name</span><input required maxLength={120} value={definition.name} onChange={(event) => changeDefinition("name", event.target.value)} /></label>
        <label className="field automation-wide"><span className="field-label">Summary</span><textarea required rows={3} maxLength={2000} value={definition.summary} onChange={(event) => changeDefinition("summary", event.target.value)} /></label>
        <label className="field"><span className="field-label">Agent type</span><SelectInput value={definition.agent_type} onChange={(event) => {
          const next = event.target.value as AgentType;
          changeDefinition("agent_type", next);
          changeDefinition("tool_policy", definition.tool_policy.filter((tool) => TOOL_OPTIONS[next].some((option) => option.id === tool)));
        }}>{AGENT_TYPES.map((option) => <option key={option.id} value={option.id}>{option.label}</option>)}</SelectInput></label>
        <label className="field"><span className="field-label">Knowledge boundary</span><SelectInput value={definition.knowledge_scope} onChange={(event) => changeDefinition("knowledge_scope", event.target.value as AgentKnowledgeScope)}><option value="requester_acl">Only documents the requesting employee can read</option><option value="department_only">Requesting employee’s readable documents in a selected department</option></SelectInput></label>
        <fieldset className="agent-tools automation-wide"><legend>Allowed capabilities</legend>{TOOL_OPTIONS[definition.agent_type].map((option) => <label className="agent-tool-option" key={option.id}><input type="checkbox" checked={definition.tool_policy.includes(option.id)} onChange={(event) => changeDefinition("tool_policy", event.target.checked ? [...new Set([...definition.tool_policy, option.id])] : definition.tool_policy.filter((tool) => tool !== option.id))} />{option.label}</label>)}</fieldset>
        <div className="automation-actions"><button className="btn btn-primary" disabled={busy === "draft" || !definition.name.trim() || !definition.summary.trim() || definition.tool_policy.length === 0}>{busy === "draft" ? "Saving…" : versionOf ? "Save immutable version" : "Save draft"}</button>{versionOf && <button className="btn btn-ghost" type="button" onClick={resetDraft}>Cancel</button>}</div>
      </form>
      <p className="small muted">Drafts are not visible to employees. Publication requires a different account with a reviewer grant.</p>
    </section>}

    {(canPublish || canReview) && <section className="approval-section" aria-label="Catalog version lifecycle">
      <h3>Catalog versions and review</h3>
      {managed.length === 0 ? <p className="muted">No templates are assigned to this catalog view.</p> : managed.map((template) => <article className="card approval-card" key={template.id}>
        <div className="approval-meta"><strong>{template.slug}</strong><span className="chip chip-sm">Published v{template.published_version ?? "—"}</span><span className={`chip chip-sm ${template.revoked_at ? "chip-muted" : ""}`}>{template.revoked_at ? "Revoked" : "Active"}</span></div>
        {template.versions.map((version) => <div className="catalog-version-row" key={version.id}>
          <div className="approval-meta"><strong>v{version.version} · {version.name}</strong><span className="chip chip-sm">{version.status.replaceAll("_", " ")}</span><span className="muted small">by {version.author_email}</span></div>
          {version.review_rationale && <p className="muted small">Review rationale: {version.review_rationale}</p>}
          {version.status === "draft" && canPublish && !template.revoked_at && <button className="btn" type="button" disabled={busy === version.id} onClick={() => void submitVersion(version.id)}>Submit for independent review</button>}
          {version.status === "pending_review" && canReview && !template.revoked_at && <div className="automation-form">
            <label className="field"><span className="field-label">Review rationale</span><textarea rows={2} maxLength={2000} value={reviewRationales[version.id] ?? ""} onChange={(event) => setReviewRationales((current) => ({ ...current, [version.id]: event.target.value }))} /></label>
            <div className="automation-actions"><button className="btn btn-primary" type="button" disabled={busy === version.id || (reviewRationales[version.id]?.trim().length ?? 0) < 3} onClick={() => void reviewVersion(version.id, "approve")}>Approve version</button><button className="btn" type="button" disabled={busy === version.id || (reviewRationales[version.id]?.trim().length ?? 0) < 3} onClick={() => void reviewVersion(version.id, "reject")}>Reject version</button></div>
          </div>}
        </div>)}
        {canPublish && !template.revoked_at && template.versions[0] && <button className="btn btn-ghost" type="button" onClick={() => startVersion(template)}>Create next version</button>}
        {canReview && !template.revoked_at && <div className="automation-form"><label className="field"><span className="field-label">Revocation rationale</span><textarea rows={2} maxLength={2000} value={revokeRationales[template.id] ?? ""} onChange={(event) => setRevokeRationales((current) => ({ ...current, [template.id]: event.target.value }))} /></label><button className="btn btn-ghost" type="button" disabled={busy === template.id || (revokeRationales[template.id]?.trim().length ?? 0) < 3} onClick={() => void revokeTemplate(template.id)}>Revoke catalog template</button></div>}
      </article>)}
    </section>}

    {user.role === "admin" && <section className="card" aria-label="Catalog publisher grants">
      <h3>Publisher and reviewer grants</h3>
      <form className="automation-actions" onSubmit={(event) => void createGrant(event)}>
        <label className="field"><span className="field-label">Account email</span><input type="email" required maxLength={320} value={grantEmail} onChange={(event) => setGrantEmail(event.target.value)} /></label>
        <label className="field"><span className="field-label">Grant</span><SelectInput value={grantPermission} onChange={(event) => setGrantPermission(event.target.value as "publish" | "review")}><option value="publish">Publisher</option><option value="review">Reviewer</option></SelectInput></label>
        <button className="btn btn-primary" disabled={busy === "grant" || !grantEmail.trim()}>Add grant</button>
      </form>
      {grants.length === 0 ? <p className="muted">No publisher or reviewer grants are recorded.</p> : <ul className="workflow-notifications">{grants.map((grant) => <li key={grant.id}><span><strong>{grant.email}</strong><span className="small muted"> · {grant.permission} · {grant.revoked_at ? "revoked" : "active"}</span></span>{!grant.revoked_at && <button className="btn btn-ghost" type="button" disabled={busy === grant.id} onClick={() => void revokeGrant(grant.id)}>Revoke</button>}</li>)}</ul>}
    </section>}
  </section>;
}
