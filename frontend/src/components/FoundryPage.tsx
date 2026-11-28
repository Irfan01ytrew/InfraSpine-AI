import { useCallback, useEffect, useState } from "react";
import { api, describeError } from "../api";
import { buildFoundryPipelineSpec } from "../lib/foundryPipeline.mjs";
import type { FoundryAppDetail, FoundryAppSummary, FoundryScope } from "../types";
import { Alert, Button, Card, EmptyState, PageHeader } from "./ui";
import { useFeedback } from "./Feedback";
import { FlowDiagram } from "../workflows/FlowDiagram";

/**
 * Step AM slice 6 — Software Foundry page: list generated apps, show one
 * app's builds + build pipeline (read-only FlowDiagram), request a deploy
 * approval, and submit feedback. Deploys never happen here: requesting a
 * deploy only creates a platform approval for the exact build hash, and a
 * different person must approve it (see Approvals).
 */
export function FoundryPage() {
  const { toast } = useFeedback();
  const [scopes, setScopes] = useState<FoundryScope[] | null>(null);
  const [scopeId, setScopeId] = useState<string>("");
  const [apps, setApps] = useState<FoundryAppSummary[] | null>(null);
  const [selectedSlug, setSelectedSlug] = useState<string>("");
  const [detail, setDetail] = useState<FoundryAppDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [selectedStage, setSelectedStage] = useState<string | undefined>(undefined);
  const [deployTarget, setDeployTarget] = useState("");
  const [deployBusy, setDeployBusy] = useState<string | null>(null);
  const [feedbackKind, setFeedbackKind] = useState("change_request");
  const [feedbackNote, setFeedbackNote] = useState("");
  const [feedbackBusy, setFeedbackBusy] = useState(false);
  const [lastRevision, setLastRevision] = useState<{ new_version: number; status: string } | null>(null);

  const loadApps = useCallback(async (scope: string) => {
    const { apps: listed } = await api.foundryApps(scope);
    setApps(listed);
    if (listed.length > 0) {
      const slug = listed[0].slug;
      setSelectedSlug(slug);
      setDetail(await api.foundryApp(scope, slug));
    } else {
      setSelectedSlug("");
      setDetail(null);
    }
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const { scopes: visible } = await api.foundryScopes();
      setScopes(visible);
      if (visible.length > 0) {
        setScopeId(visible[0].id);
        await loadApps(visible[0].id);
      }
      setError(null);
    } catch (err) {
      setError(describeError(err));
    } finally {
      setLoading(false);
    }
  }, [loadApps]);

  useEffect(() => {
    void load();
  }, [load]);

  async function selectApp(slug: string) {
    if (!scopeId) return;
    setSelectedSlug(slug);
    try {
      setDetail(await api.foundryApp(scopeId, slug));
    } catch (err) {
      setError(describeError(err));
    }
  }

  async function requestDeploy(buildFingerprint: string) {
    if (!scopeId || !detail || !deployTarget.trim()) {
      toast("error", "Deploy target required", "Ask the owner which target generated apps may deploy to.");
      return;
    }
    setDeployBusy(buildFingerprint);
    try {
      const result = await api.foundryDeployRequest(scopeId, detail.slug, buildFingerprint, deployTarget.trim());
      toast(
        "success",
        "Deploy approval requested",
        `Approval ${result.approval_id.slice(0, 8)}… covers build ${result.action_hash.slice(0, 12)}…. A different person must approve it before anything deploys.`,
      );
      await selectApp(detail.slug);
    } catch (err) {
      toast("error", "Deploy request failed", describeError(err));
    } finally {
      setDeployBusy(null);
    }
  }

  async function submitFeedback(buildFingerprint: string) {
    if (!scopeId || !detail || !feedbackNote.trim()) {
      toast("error", "Feedback note required", "Write what should change, or what went wrong.");
      return;
    }
    setFeedbackBusy(true);
    try {
      const result = await api.foundryFeedback(scopeId, detail.slug, {
        build_fingerprint: buildFingerprint,
        kind: feedbackKind,
        note: feedbackNote.trim(),
        changes: [],
      });
      setLastRevision(result.revision ? { new_version: result.revision.new_version, status: result.revision.status } : null);
      toast(
        "success",
        "Feedback recorded",
        result.revision
          ? `Revision v${result.revision.new_version} is ${result.revision.status}: it must be re-tested and re-approved before it deploys.`
          : "Notes without a structured change are stored for a human to read.",
      );
      setFeedbackNote("");
      await selectApp(detail.slug);
    } catch (err) {
      toast("error", "Feedback could not be recorded", describeError(err));
    } finally {
      setFeedbackBusy(false);
    }
  }

  if (loading) {
    return (
      <div>
        <PageHeader title="Software Foundry" description="Apps the Foundry built: spec, test, preview, approval, deploy, feedback." />
        <Card><p className="muted">Loading generated apps…</p></Card>
      </div>
    );
  }

  return (
    <div>
      <PageHeader title="Software Foundry" description="Apps the Foundry built: spec, test, preview, approval, deploy, feedback." />
      {error ? <Alert tone="danger">{error}</Alert> : null}
      {scopes && scopes.length === 0 ? (
        <EmptyState title="No Foundry access" description="No scope grants you foundry.read. Ask an administrator for access." />
      ) : (
        <>
          {scopes && scopes.length > 1 ? (
            <Card>
              <label className="muted" htmlFor="foundry-scope">Scope</label>
              <select
                id="foundry-scope"
                value={scopeId}
                onChange={(event) => {
                  setScopeId(event.target.value);
                  void loadApps(event.target.value);
                }}
              >
                {scopes.map((scope) => (
                  <option key={scope.id} value={scope.id}>{scope.name} ({scope.kind})</option>
                ))}
              </select>
            </Card>
          ) : null}
          {!apps || apps.length === 0 ? (
            <EmptyState title="No generated apps yet" description="When the Foundry finishes a build, it appears here with its test results, preview and deploy state." />
          ) : (
            <div className="foundry-layout">
              <Card>
                <h2>Generated apps</h2>
                <table className="table">
                  <thead>
                    <tr><th>App</th><th>Builds</th><th>Latest</th><th>Gate</th><th>Deploy</th><th>Feedback</th></tr>
                  </thead>
                  <tbody>
                    {apps.map((app) => (
                      <tr key={app.slug} className={app.slug === selectedSlug ? "selected" : undefined} onClick={() => void selectApp(app.slug)}>
                        <td>{app.name}</td>
                        <td>{app.builds}</td>
                        <td>{app.latest_version === null ? "—" : `v${app.latest_version}`}</td>
                        <td>{app.latest_gate ?? "—"}</td>
                        <td>{app.latest_deploy_state ?? "—"}</td>
                        <td>{app.feedback_count}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </Card>
              {detail ? (
                <Card>
                  <h2>{detail.name} — build pipeline</h2>
                  <p className="muted">Read-only: the pipeline shows where the latest build stands. Nothing here deploys or edits a live app.</p>
                  <FlowDiagram spec={buildFoundryPipelineSpec(detail)} selected={selectedStage} onSelect={setSelectedStage} />
                  <h3>Builds</h3>
                  <table className="table">
                    <thead>
                      <tr><th>Version</th><th>Gate</th><th>Health</th><th>Tests</th><th>Deploy</th><th></th></tr>
                    </thead>
                    <tbody>
                      {detail.builds.map((build) => (
                        <tr key={build.build_fingerprint}>
                          <td>v{build.spec_version}</td>
                          <td>{build.gate}</td>
                          <td>{build.health}</td>
                          <td>{build.tests_passed} passed, {build.tests_failed} failed</td>
                          <td>{build.deploy_state}{build.deploy_target ? ` → ${build.deploy_target}` : ""}</td>
                          <td>
                            {build.gate === "preview_ready" && build.deploy_state === "none" ? (
                              <span>
                                <input
                                  aria-label="Deploy target"
                                  placeholder="Deploy target"
                                  value={deployTarget}
                                  onChange={(event) => setDeployTarget(event.target.value)}
                                />
                                <Button disabled={deployBusy === build.build_fingerprint} onClick={() => void requestDeploy(build.build_fingerprint)}>
                                  {deployBusy === build.build_fingerprint ? "Requesting…" : "Request deploy"}
                                </Button>
                              </span>
                            ) : null}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  <h3>Feedback</h3>
                  {detail.feedback.length === 0 ? (
                    <p className="muted">No feedback yet for this app.</p>
                  ) : (
                    <ul>
                      {detail.feedback.map((item) => (
                        <li key={item.id}><strong>{item.kind}</strong> — {item.note}</li>
                      ))}
                    </ul>
                  )}
                  {lastRevision ? (
                    <Alert tone="info">Revision v{lastRevision.new_version} is {lastRevision.status}. It must be re-tested and re-approved before it deploys; the live app keeps serving the previous build.</Alert>
                  ) : null}
                  {detail.builds.length > 0 ? (
                    <div>
                      <label className="muted" htmlFor="foundry-feedback-kind">Kind</label>
                      <select id="foundry-feedback-kind" value={feedbackKind} onChange={(event) => setFeedbackKind(event.target.value)}>
                        <option value="change_request">Change request</option>
                        <option value="bug">Bug</option>
                        <option value="praise">Praise</option>
                        <option value="question">Question</option>
                      </select>
                      <label className="muted" htmlFor="foundry-feedback-note">Note</label>
                      <textarea
                        id="foundry-feedback-note"
                        value={feedbackNote}
                        onChange={(event) => setFeedbackNote(event.target.value)}
                        placeholder="What should change, or what went wrong?"
                      />
                      <Button
                        disabled={feedbackBusy}
                        onClick={() => void submitFeedback(detail.builds[detail.builds.length - 1].build_fingerprint)}
                      >
                        {feedbackBusy ? "Recording…" : "Submit feedback"}
                      </Button>
                    </div>
                  ) : null}
                </Card>
              ) : null}
            </div>
          )}
        </>
      )}
    </div>
  );
}
