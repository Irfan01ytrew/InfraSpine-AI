import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";
import { api, describeError } from "../api";
import { formatDate, mediaPreviewHref } from "../format";
import { IconActivity, IconAlert, Spinner } from "../icons";
import type { ForecastPeriodUnit, ForecastScenario, ForecastScenarioSummary, RagDocument } from "../types";
import { Alert, Button, PageHeader, SelectInput } from "./ui";
import { useLiveRefresh } from "../lib/useLiveRefresh";

interface ObservationDraft {
  period: string;
  value: string;
  documentId: string;
  excerpt: string;
}

const emptyObservation = (): ObservationDraft => ({ period: "", value: "", documentId: "", excerpt: "" });

function displayNumber(value: string | number): string {
  const amount = typeof value === "number" ? value : Number(value);
  return Number.isFinite(amount) ? new Intl.NumberFormat(undefined, { maximumFractionDigits: 6 }).format(amount) : String(value);
}

export function ForecastsPage() {
  const [documents, setDocuments] = useState<RagDocument[]>([]);
  const [scenarios, setScenarios] = useState<ForecastScenarioSummary[]>([]);
  const [detail, setDetail] = useState<ForecastScenario | null>(null);
  const [title, setTitle] = useState("");
  const [metric, setMetric] = useState("");
  const [unit, setUnit] = useState("");
  const [periodUnit, setPeriodUnit] = useState<ForecastPeriodUnit>("year");
  const [growth, setGrowth] = useState("0");
  const [adjustment, setAdjustment] = useState("0");
  const [horizon, setHorizon] = useState("3");
  const [notes, setNotes] = useState("");
  const [observations, setObservations] = useState<ObservationDraft[]>([emptyObservation(), emptyObservation()]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const refresh = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    try {
      const [documentResponse, scenarioResponse] = await Promise.all([api.documents(), api.forecastScenarios()]);
      setDocuments(documentResponse.documents.filter((document) => document.status === "ready" && document.searchable));
      setScenarios(scenarioResponse.items);
      setError("");
    } catch (reason) {
      setError(describeError(reason));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);
  useLiveRefresh(() => refresh(true));

  function changeObservation(index: number, changes: Partial<ObservationDraft>) {
    setObservations((current) => current.map((item, itemIndex) => itemIndex === index ? { ...item, ...changes } : item));
  }

  async function create(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const created = await api.createForecastScenario({
        title,
        metric,
        unit,
        period_unit: periodUnit,
        observations: observations.map((item) => ({
          period: item.period,
          value: item.value,
          document_id: item.documentId || documents[0]?.id || "",
          excerpt: item.excerpt,
        })),
        growth_rate_pct: growth,
        additive_adjustment: adjustment,
        horizon_periods: Number(horizon),
        assumption_notes: notes,
      });
      setDetail(created);
      setTitle("");
      setNotes("");
      setObservations([emptyObservation(), emptyObservation()]);
      setNotice("Immutable scenario saved. Re-run with revised assumptions to create a new reproducible version.");
      await refresh();
    } catch (reason) {
      setError(describeError(reason));
    } finally {
      setBusy(false);
    }
  }

  async function openScenario(item: ForecastScenarioSummary) {
    setBusy(true);
    setError("");
    try {
      setDetail(await api.forecastScenario(item.id));
    } catch (reason) {
      setError(describeError(reason));
    } finally {
      setBusy(false);
    }
  }

  const usableDocuments = documents;

  return <div className="forecast-page">
    <PageHeader
      title="What-if forecasts"
      description="Build transparent projections from historical values that you can cite to readable, indexed source documents."
      actions={<><Button onClick={() =>void refresh()} disabled={loading || busy}><IconActivity size={15} />Refresh</Button></>}
    />

    <Alert tone="info" className="mb-4 forecast-explainer">This is deterministic arithmetic, not a statistical forecast or confidence interval. Historical values must appear in the exact source excerpt. The saved model version, assumptions and input digest make each scenario repeatable.</Alert>
    {error && <div className="inline-alert inline-alert-error" role="alert"><IconAlert size={17} />{error}</div>}
    {notice && <div className="inline-alert" role="status">{notice}</div>}

    <section className="card forecast-card">
      <div className="card-title"><div><h2>Create scenario</h2><p className="muted">Enter historical periods oldest first. Use a source excerpt that includes both the period and value.</p></div></div>
      {loading ? <div className="loading"><Spinner size={20} /></div> : usableDocuments.length === 0 ? (
        <p className="empty">No readable, indexed documents are ready. Upload or select an authorized source in Knowledge first.</p>
      ) : <form className="forecast-form" onSubmit={(event) => void create(event)}>
        <div className="forecast-fields">
          <label>Scenario name<input value={title} onChange={(event) => setTitle(event.target.value)} maxLength={160} required /></label>
          <label>Metric<input value={metric} onChange={(event) => setMetric(event.target.value)} placeholder="Revenue, cost, volume" maxLength={100} required /></label>
          <label>Unit<input value={unit} onChange={(event) => setUnit(event.target.value)} placeholder="USD, units, hours" maxLength={32} required /></label>
          <label>Period unit<SelectInput value={periodUnit} onChange={(event) => setPeriodUnit(event.target.value as ForecastPeriodUnit)}>
            <option value="year">Year</option><option value="quarter">Quarter</option><option value="month">Month</option><option value="period">Custom period</option>
          </SelectInput></label>
          <label>Growth per period (%)<input type="number" step="any" min="-100" max="500" value={growth} onChange={(event) => setGrowth(event.target.value)} required /></label>
          <label>Additive adjustment per period<input type="number" step="any" value={adjustment} onChange={(event) => setAdjustment(event.target.value)} required /></label>
          <label>Forecast horizon<input type="number" min="1" max="36" value={horizon} onChange={(event) => setHorizon(event.target.value)} required /></label>
          <label className="forecast-notes">Assumption notes<textarea value={notes} onChange={(event) => setNotes(event.target.value)} maxLength={1000} rows={2} placeholder="State the business assumption behind this scenario." /></label>
        </div>

        <div className="forecast-observations">
          <div className="forecast-observations-heading"><div><h3>Historical values</h3><p className="muted">Each value is validated against the exact quoted passage and the source’s current access/version.</p></div>
            <button className="btn btn-secondary" type="button" disabled={observations.length >= 24} onClick={() => setObservations((current) => [...current, emptyObservation()])}>Add period</button>
          </div>
          {observations.map((item, index) => <fieldset className="forecast-observation" key={index}>
            <legend>Observation {index + 1}</legend>
            <div className="forecast-observation-fields">
              <label>Period<input value={item.period} onChange={(event) => changeObservation(index, { period: event.target.value })} maxLength={48} placeholder="2024 or 2024-Q1" required /></label>
              <label>Value<input type="number" step="any" value={item.value} onChange={(event) => changeObservation(index, { value: event.target.value })} required /></label>
              <label className="forecast-source">Readable source<SelectInput value={item.documentId || usableDocuments[0]?.id || ""} onChange={(event) => changeObservation(index, { documentId: event.target.value })} required>
                {usableDocuments.map((document) => <option value={document.id} key={document.id}>{document.filename} · v{document.version}</option>)}
              </SelectInput></label>
              <label className="forecast-excerpt">Exact source excerpt<textarea value={item.excerpt} onChange={(event) => changeObservation(index, { excerpt: event.target.value })} minLength={8} maxLength={800} rows={3} required placeholder="Copy the exact passage from this document, including the period and number." /></label>
              <div className="forecast-row-actions">
                <a href="#/knowledge" className="btn btn-ghost">Open Knowledge to read source</a>
                {observations.length > 2 && <button className="btn btn-ghost" type="button" onClick={() => setObservations((current) => current.filter((_, itemIndex) => itemIndex !== index))}>Remove period</button>}
              </div>
            </div>
          </fieldset>)}
        </div>
        <button className="btn btn-primary" type="submit" disabled={busy || observations.length < 2}>
          {busy ? <><Spinner size={14} />Calculating</> : "Calculate and save immutable scenario"}
        </button>
      </form>}
    </section>

    {detail && <section className="card forecast-card" aria-label="Forecast scenario details">
      <div className="card-title"><div><h2>{detail.title}</h2><p className="muted">{detail.metric} · {detail.unit} · {detail.model_version} · saved {formatDate(detail.created_at)}</p></div></div>
      <div className="forecast-assumption-summary"><strong>Assumptions</strong><span>{displayNumber(detail.assumptions.growth_rate_pct)}% growth</span><span>{displayNumber(detail.assumptions.additive_adjustment)} adjustment per period</span><span>{detail.assumptions.horizon_periods} periods</span>{detail.assumptions.notes && <p>{detail.assumptions.notes}</p>}</div>
      <div className="table-wrap"><table><thead><tr><th>Forecast period</th><th>{detail.metric} ({detail.unit})</th></tr></thead><tbody>
        {detail.projections.map((projection) => <tr key={projection.period}><td>{projection.period}</td><td>{displayNumber(projection.value)}</td></tr>)}
      </tbody></table></div>
      <h3>Historical evidence</h3>
      <ul className="forecast-citations">{detail.observations.map((observation, index) => {
        const citation = observation.citation;
        const mediaPreview = mediaPreviewHref({ document_id: citation.document_id, filename: citation.filename, locator: citation.locator });
        const locatorKind = typeof citation.locator?.kind === "string" ? citation.locator.kind : null;
        const pagePreview = (locatorKind === null || locatorKind === "page") && /\.(pdf|png|jpe?g)$/i.test(citation.filename)
          ? `/api/rag/documents/${encodeURIComponent(citation.document_id)}/pages/${citation.page_number}/image`
          : null;
        return <li key={`${citation.document_id}-${observation.period}-${index}`}>
          <div><strong>{observation.period}: {displayNumber(observation.value)}</strong><small className="muted">{citation.filename} · page/section {citation.page_number} · document v{citation.document_version}</small>
            <blockquote>{citation.excerpt}</blockquote>
          </div>
          {mediaPreview && <a className="source-page-link" href={mediaPreview} target="_blank" rel="noopener noreferrer">Open original media at cited timestamp ↗</a>}
          {!mediaPreview && pagePreview && <a className="source-page-link" href={pagePreview} target="_blank" rel="noopener noreferrer">Open cited source page ↗</a>}
        </li>;
      })}</ul>
      <p className="forecast-digest"><span>Reproducibility digest</span><code>{detail.input_digest}</code></p>
    </section>}

    <section className="card forecast-card">
      <div className="card-title"><div><h2>Saved scenarios</h2><p className="muted">Private to your account; scenarios with revoked or changed source evidence are hidden.</p></div><span className="pill pill-sm">{scenarios.length}</span></div>
      {loading ? <div className="loading"><Spinner size={20} /></div> : scenarios.length === 0 ? <p className="empty">No saved scenarios yet.</p> : <div className="forecast-scenario-list">
        {scenarios.map((scenario) => <article className="forecast-scenario-item" key={scenario.id}>
          <div><strong>{scenario.title}</strong><small className="muted">{scenario.metric} · {scenario.unit} · {scenario.observation_count} cited values · {scenario.projections.length} projected periods</small></div>
          <button className="btn btn-secondary" disabled={busy} onClick={() => void openScenario(scenario)}>Open</button>
        </article>)}
      </div>}
    </section>
  </div>;
}
