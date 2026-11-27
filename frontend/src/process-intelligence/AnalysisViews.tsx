import { useState } from "react";
import { request } from "../api";
import { date, hours, processApi } from "./api";
import type { Case, Model, Simulation } from "./types";

export function VariantExplorer({ model, cases, onEvidence }: { model: Model; cases: Case[]; onEvidence: (ids: string[]) => void }) {
  const [selected, setSelected] = useState<string | null>(null);
  const [caseKey, setCaseKey] = useState<string | null>(null);
  const matches = cases.filter(c => !selected || JSON.stringify(c.sequence) === selected);
  const activeCase = matches.find(c => c.source_id + c.case_id === caseKey) ?? matches[0];
  return <div className="pi-split"><section className="pi-card"><div className="pi-card-head"><h2>Process variants</h2><button onClick={() => setSelected(null)}>All variants</button></div><div className="pi-list">
    {model.data.variants.map(v => { const key = JSON.stringify(v.sequence); const count = cases.filter(c => JSON.stringify(c.sequence) === key).length; return count ? <button key={v.id} className={selected === key ? "pi-selected" : ""} onClick={() => { setSelected(key); setCaseKey(null); }}><span className="pi-row"><strong>{v.id.replace("variant-", "Variant ")}</strong><span>{count} cases</span></span><span>{v.sequence.join(" → ")}</span></button> : null; })}
    {!model.data.variants.length && <p className="pi-empty">No unambiguous variants yet.</p>}
  </div></section><section className="pi-card"><div className="pi-card-head"><h2>Execution timeline</h2><select aria-label="Execution case" value={activeCase ? activeCase.source_id + activeCase.case_id : ""} onChange={e => setCaseKey(e.target.value)}>{matches.map(c => <option key={c.source_id+c.case_id} value={c.source_id+c.case_id}>{c.case_id}</option>)}</select></div>
    {activeCase ? <><div className="pi-card-pad pi-row"><span>{activeCase.completed ? "Completed" : "Open"}{activeCase.ambiguous ? " · Ambiguous ordering" : ""}</span><strong>{hours(activeCase.cycle_hours)}</strong></div><ol className="pi-timeline">{activeCase.event_ids.map(id => { const e = model.data.evidence[id]; return <li key={id}><button onClick={() => onEvidence([id])}><strong>{e.activity}</strong><span>{date(e.occurred_at)}</span><span>{e.actor ?? "Owner unrecorded"} · {e.department ?? "Department unrecorded"}</span></button></li>; })}</ol></> : <p className="pi-empty">No executions match the filters.</p>}
  </section></div>;
}

export function Findings({ model, cases, onEvidence }: { model: Model; cases: Case[]; onEvidence: (ids: string[]) => void }) {
  const [kind, setKind] = useState("");
  const ids = new Set(cases.flatMap(c => c.event_ids));
  const findings = model.data.findings.filter(f => (!kind || f.kind === kind) && f.evidence_ids.some(id => ids.has(id)));
  return <section className="pi-card"><div className="pi-card-head"><div><h2>Findings & root-cause investigation</h2><p>Observed symptoms and explicitly labeled hypotheses</p></div><select aria-label="Finding category" value={kind} onChange={e => setKind(e.target.value)}><option value="">All findings</option>{[...new Set(model.data.findings.map(f => f.kind))].map(k => <option key={k}>{k}</option>)}</select></div>
    <div className="pi-findings">{findings.map(f => <article key={f.id}><div className="pi-row"><span className={`pi-badge ${f.kind === "sla" || f.kind === "conformance" ? "pi-warning" : ""}`}>{f.kind}</span><span className="pi-muted">{f.classification}</span></div><h3>{f.title}</h3><p>{f.case_id ?? f.activity ?? f.transition?.join(" → ") ?? "Process scope"}{f.hours !== undefined ? ` · ${hours(f.hours)}` : ""}</p>{f.qualification && <p>{f.qualification}</p>}{f.deviations?.map((d, i) => <p key={i}>{d.kind}: {d.activity}{d.expected ? ` → expected ${d.expected}` : ""}</p>)}<button onClick={() => onEvidence(f.evidence_ids)}>Inspect {new Set(f.evidence_ids).size} evidence events</button></article>)}</div>
    {!findings.length && <p className="pi-empty">No findings in this scope.</p>}
  </section>;
}

export function Recommendations({ model, path, onEvidence, onError }: { model: Model; path: string; onEvidence: (ids: string[]) => void; onError: (error: unknown) => void }) {
  const [question, setQuestion] = useState(""); const [answer, setAnswer] = useState(""); const [busy, setBusy] = useState(false);
  return <section className="pi-card"><div className="pi-card-head"><div><h2>Improvement proposals</h2><p>Evidence-based hypotheses for human review</p></div></div><div className="pi-findings">{model.data.recommendations.map(r => <article key={r.id}><span className="pi-badge">Proposed · inference</span><h3>{r.title}</h3><button onClick={() => onEvidence(r.evidence_ids)}>Review supporting evidence</button></article>)}</div><form className="pi-card-pad" onSubmit={async e => { e.preventDefault(); setBusy(true); setAnswer(""); try { const result = await request<{ answer: string }>("POST", `${path}/ask`, { model_id: model.id, question }); setAnswer(result.answer); } catch (err) { onError(err); } finally { setBusy(false); } }}><label>Ask the configured AI analyst<input value={question} onChange={e => setQuestion(e.target.value)} required minLength={3} maxLength={2000} placeholder="Which findings should we investigate first?" /></label><button className="pi-primary" disabled={busy}>{busy ? "Analyzing…" : "Ask analyst"}</button>{answer && <div className="pi-ai-answer"><span className="pi-badge">AI inference · verify before use</span><p>{answer}</p></div>}</form></section>;
}

export function SimulationWorkspace({ model, path, onError, onEvidence }: { model: Model; path: string; onError: (error: unknown) => void; onEvidence: (ids: string[]) => void }) {
  const [actor, setActor] = useState(model.data.owners[0]?.name ?? ""); const [absence, setAbsence] = useState(8); const [capacity, setCapacity] = useState(0); const [result, setResult] = useState<Simulation | null>(null); const [busy, setBusy] = useState(false);
  return <div className="pi-split"><section className="pi-card"><div className="pi-card-head"><div><h2>Manager / owner absence</h2><p>Replay the observed case mix with explicit assumptions</p></div><span className="pi-badge pi-warning">Hypothetical</span></div><form className="pi-card-pad pi-form" onSubmit={async e => { e.preventDefault(); setBusy(true); try { setResult(await processApi.simulate(path, { model_id: model.id, actor, absence_hours: absence, replacement_capacity: capacity / 100 })); } catch (err) { onError(err); } finally { setBusy(false); } }}>
    <label>Observed owner<select value={actor} onChange={e => { setActor(e.target.value); setResult(null); }} required>{model.data.owners.map(o => <option key={o.name}>{o.name}</option>)}</select></label>
    <label>Absence duration (hours)<input type="number" min="0.1" max="8760" step="0.1" value={absence} onChange={e => { setAbsence(Number(e.target.value)); setResult(null); }} required /></label>
    <label>Replacement capacity (%)<input type="number" min="0" max="100" value={capacity} onChange={e => { setCapacity(Number(e.target.value)); setResult(null); }} required /></label>
    <button className="pi-primary" disabled={busy || !actor}>{busy ? "Simulating…" : "Run simulation"}</button></form></section>
    <section className="pi-card"><div className="pi-card-head"><h2>Estimated impact</h2></div>{result ? <div className="pi-card-pad"><div className="pi-metrics pi-metrics-small"><div><span>Affected cases</span><strong>{result.affected_cases}</strong></div><div><span>Added time per case</span><strong>{hours(result.additional_hours_per_affected_case)}</strong></div><div><span>Additional SLA breaches</span><strong>{result.additional_sla_breaches ?? "—"}</strong></div></div><ul className="pi-notes">{result.assumptions.map(a => <li key={a}>{a}</li>)}</ul><button onClick={() => onEvidence(result.evidence_ids)}>Inspect affected evidence</button></div> : <p className="pi-empty">Select an observed owner and run a scenario to estimate its impact.</p>}</section></div>;
}

export function Performance({ model, cases }: { model: Model; cases: Case[] }) {
  const ids = new Set(cases.flatMap(c => c.event_ids)); const days = new Map<string, number>();
  cases.filter(c => c.completed).forEach(c => days.set(c.end.slice(0, 10), (days.get(c.end.slice(0, 10)) ?? 0) + 1));
  const maximum = Math.max(1, ...days.values());
  return <div className="pi-split"><section className="pi-card"><div className="pi-card-head"><h2>Completed throughput</h2><span className="pi-muted">UTC · cases/day</span></div><div className="pi-bars">{[...days].sort().map(([d, n]) => <div key={d}><span>{d}</span><meter min="0" max={maximum} value={n} aria-label={`${d}: ${n} completed cases`} /><strong>{n}</strong></div>)}{!days.size && <p className="pi-empty">No completed cases.</p>}</div></section><section className="pi-card"><div className="pi-card-head"><h2>Department & owner activity</h2></div><div className="pi-table-wrap"><table><thead><tr><th>Department / owner</th><th>Events in scope</th><th>Measured service</th></tr></thead><tbody>{[...model.data.departments, ...model.data.owners].map((r, i) => { const events = r.evidence_ids.filter(id => ids.has(id)).map(id => model.data.evidence[id]); const durations = events.filter(e => e.finished_at).map(e => (new Date(e.finished_at!).getTime() - new Date(e.occurred_at).getTime()) / 3600000); return events.length ? <tr key={i}><td>{r.name}</td><td>{events.length}</td><td>{durations.length ? hours(durations.reduce((a, b) => a + b, 0)) : "Not recorded"}</td></tr> : null; })}</tbody></table></div><p className="pi-footnote">Service totals describe events, not resource utilization. Capacity and calendars have not been supplied.</p></section></div>;
}
