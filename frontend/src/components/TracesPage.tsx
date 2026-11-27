import { useCallback, useEffect, useState } from "react";
import { api, describeError } from "../api";
import { formatDate, timeAgo } from "../format";
import { IconThumbDown, IconThumbUp, Spinner } from "../icons";
import type { TraceDetail, TraceRow, TraceSummary } from "../types";
import { Alert, Button, PageHeader } from "./ui";
import { useLiveRefresh } from "../lib/useLiveRefresh";

const DECISIONS: Record<string, string> = {
  supported: "Answered",
  withholding: "Answered, value withheld",
  verified: "Answered after verification",
  refused_verifier: "Refused by verifier",
  refused_unsupported: "Refused, not supported",
  refused_inconclusive: "Refused, inconclusive",
  refused_no_passages: "Refused, no passages",
  refused_model_insufficient: "Refused by model",
  refused_uncited: "Refused, no citations",
  refused_uncited_document_question: "Refused, uncited",
  refused_empty: "Refused, empty reply",
  general_no_passages: "General, no passages",
  general_after_insufficient: "General, after refusal",
  general_after_unsupported: "General, after unsupported",
  general_after_inconclusive: "General, after inconclusive",
  general_uncited: "General, uncited",
  general_followup: "General follow-up",
  chart: "Chart",
  chart_refused: "Chart refused",
  page_images: "Page images",
  small_talk: "Small talk",
  memory_command: "Memory command",
  no_provider: "No provider",
};

const STAGES = ["memory", "retrieval", "answer", "verify", "llm"];

function decisionLabel(trace: TraceRow): string {
  if (trace.status === "error") return trace.error_type?.split(".").at(-1) ?? "Error";
  return trace.decision ? DECISIONS[trace.decision] ?? trace.decision.replace(/_/g, " ") : "Unknown";
}

function tone(trace: TraceRow): string {
  if (trace.status === "error") return "pill-error";
  if (trace.insufficient_evidence) return "pill-warning";
  if (trace.general_knowledge) return "";
  return "pill-success";
}

function ms(value: number | null | undefined): string {
  if (value === null || value === undefined) return "–";
  return value >= 1000 ? `${(value / 1000).toFixed(1)} s` : `${Math.round(value)} ms`;
}

function share(part: number, whole: number): string {
  return whole ? `${Math.round((part / whole) * 100)}%` : "–";
}

export function TracesPage() {
  const [days, setDays] = useState(7);
  const [status, setStatus] = useState("all");
  const [outcome, setOutcome] = useState("all");
  const [summary, setSummary] = useState<TraceSummary | null>(null);
  const [traces, setTraces] = useState<TraceRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [openId, setOpenId] = useState<number | null>(null);

  const load = useCallback(async () => {
    try {
      const result = await api.traces({ days, status, outcome });
      setSummary(result.summary);
      setTraces(result.traces);
      setError(null);
    } catch (err) {
      setError(describeError(err));
    }
  }, [days, status, outcome]);

  useEffect(() => {
    void load();
  }, [load]);
  useLiveRefresh(load);

  return (
    <>
      <PageHeader
        title="Knowledge Quality"
        actions={<><Button onClick={() =>void load()}>
          Refresh</Button></>}
      />
      {error ? (
        <Alert tone="danger" className="mb-4">{error}</Alert>
      ) : !summary ? (
        <div className="loading">
          <Spinner size={22} />
        </div>
      ) : (
        <>
          <div className="stats trace-stats">
            <Stat label="Questions" value={String(summary.questions)} hint={`last ${summary.days} days`} />
            <Stat label="Errors" value={share(summary.errors, summary.questions)} hint={`${summary.errors}`} />
            <Stat label="Refused" value={share(summary.refused, summary.questions)} hint={`${summary.refused}`} />
            <Stat label="General" value={share(summary.general, summary.questions)} hint={`${summary.general}`} />
            <Stat label="Latency p50" value={ms(summary.latency_p50_ms)} hint={`p95 ${ms(summary.latency_p95_ms)}`} />
            <Stat label="Feedback" value={`${summary.helpful} / ${summary.not_helpful}`} hint="helpful / not" />
            <Stat
              label="Tokens"
              value={(summary.prompt_tokens + summary.completion_tokens).toLocaleString()}
              hint={`${summary.llm_calls} LLM calls`}
            />
          </div>
          {summary.decisions.length > 0 && (
            <div className="chips trace-decisions">
              {summary.decisions.map((d) => (
                <span key={d.decision} className="chip chip-sm">
                  {DECISIONS[d.decision] ?? d.decision.split(".").at(-1)?.replace(/_/g, " ")} · {d.count}
                </span>
              ))}
            </div>
          )}
          {(summary.daily?.length ?? 0) > 0 && (
            <details className="card mb-4 p-4">
              <summary className="cursor-pointer font-medium">Daily outcomes in UTC</summary>
              <p className="my-3 text-sm text-muted">Refusals include expected evidence limits. Use scored QA cases to measure false refusals.</p>
              <div className="overflow-x-auto">
                <table className="w-full text-left text-sm">
                  <thead><tr><th>Date</th><th>Questions</th><th>Errors</th><th>Refusals</th><th>Missing citations</th></tr></thead>
                  <tbody>{summary.daily?.map(day => <tr key={day.day}>
                    <th scope="row"><time dateTime={day.day}>{day.day}</time></th><td>{day.questions}</td>
                    <td>{day.errors}</td><td>{day.refused}</td><td>{day.citation_refusals}</td>
                  </tr>)}</tbody>
                </table>
              </div>
            </details>
          )}
          <section className="card card-flush">
            <div className="log-toolbar">
              <Segmented
                label="Period"
                value={String(days)}
                options={[
                  ["1", "24 h"],
                  ["7", "7 days"],
                  ["30", "30 days"],
                ]}
                onChange={(v) => setDays(Number(v))}
              />
              <Segmented
                label="Outcome"
                value={outcome}
                options={[
                  ["all", "All"],
                  ["answered", "Answered"],
                  ["refused", "Refused"],
                  ["general", "General"],
                ]}
                onChange={setOutcome}
              />
              <Segmented
                label="Status"
                value={status}
                options={[
                  ["all", "All"],
                  ["error", "Errors"],
                ]}
                onChange={setStatus}
              />
            </div>
            {traces.length === 0 ? (
              <p className="empty">No questions in this period.</p>
            ) : (
              <ul className="timeline">
                {traces.map((t) => (
                  <li key={t.id} className="timeline-item trace-item">
                    <span className={`dot ${t.status === "error" ? "dot-error" : t.insufficient_evidence ? "dot-warning" : "dot-success"}`} />
                    <div className="timeline-body">
                      <button
                        type="button"
                        className="trace-toggle"
                        aria-expanded={openId === t.id}
                        onClick={() => setOpenId(openId === t.id ? null : t.id)}
                      >
                        <span className={`pill pill-sm ${tone(t)}`}>{decisionLabel(t)}</span>
                        <span className="muted">{t.user_email ?? "deleted user"}</span>
                        {t.feedback === 1 && <IconThumbUp size={13} className="trace-feedback-up" />}
                        {t.feedback === -1 && <IconThumbDown size={13} className="trace-feedback-down" />}
                      </button>
                      <div className="timeline-meta">
                        <span>{ms(t.latency_ms)}</span>
                        <span>· {t.strategy ?? "no strategy"}</span>
                        <span>
                          · {t.cited_count}/{t.retrieved_count} cited
                        </span>
                        <span>· {t.llm_calls} LLM</span>
                        {t.model && <span className="chip chip-sm">{t.model.replace(/^models\//, "")}</span>}
                      </div>
                      {openId === t.id && <TraceDetails id={t.id} />}
                    </div>
                    <time className="timeline-time" dateTime={t.created_at} title={formatDate(t.created_at)}>
                      {timeAgo(t.created_at)}
                    </time>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </>
      )}
    </>
  );
}

function Stat({ label, value, hint }: { label: string; value: string; hint: string }) {
  return (
    <div className="stat">
      <span className="stat-label">{label}</span>
      <span className="stat-value">{value}</span>
      <span className="stat-hint">{hint}</span>
    </div>
  );
}

function Segmented({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: [string, string][];
  onChange: (value: string) => void;
}) {
  return (
    <div className="segmented" role="radiogroup" aria-label={label}>
      {options.map(([v, text]) => (
        <button
          key={v}
          type="button"
          role="radio"
          aria-checked={value === v}
          className={value === v ? "segment segment-active" : "segment"}
          onClick={() => onChange(v)}
        >
          {text}
        </button>
      ))}
    </div>
  );
}

function TraceDetails({ id }: { id: number }) {
  const [detail, setDetail] = useState<TraceDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.trace(id).then(setDetail, (err) => setError(describeError(err)));
  }, [id]);

  if (error) return <p className="trace-error">{error}</p>;
  if (!detail) return <Spinner size={14} />;
  const stages = STAGES.filter((name) => detail.timings[name] !== undefined);
  return (
    <div className="trace-detail">
      <dl className="trace-facts">
        <dt>Request</dt>
        <dd className="mono">{detail.request_id ?? "–"}</dd>
        <dt>Route</dt>
        <dd>
          {detail.route} · {detail.history_turns} earlier turns · {detail.question_chars} characters
        </dd>
        {stages.length > 0 && (
          <>
            <dt>Stages</dt>
            <dd>{stages.map((name) => `${name} ${ms(detail.timings[name])}`).join(" · ")}</dd>
          </>
        )}
        {(detail.prompt_tokens !== null || detail.completion_tokens !== null) && (
          <>
            <dt>Tokens</dt>
            <dd>
              {detail.prompt_tokens ?? 0} in · {detail.completion_tokens ?? 0} out
            </dd>
          </>
        )}
      </dl>
      {detail.retrieved.length > 0 && (
        <table className="trace-chunks">
          <thead>
            <tr>
              <th>#</th>
              <th>Document</th>
              <th>Score</th>
              <th>Cited</th>
            </tr>
          </thead>
          <tbody>
            {detail.retrieved.map((c) => (
              <tr key={`${c.rank}-${c.chunk_id}`}>
                <td>{c.rank}</td>
                <td>{c.filename ?? "deleted document"}</td>
                <td className="mono">{c.score.toFixed(3)}</td>
                <td>{c.cited ? "yes" : ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {detail.error_stack && (
        <pre className="trace-stack">{[detail.error_type, detail.error_stack].filter(Boolean).join("\n")}</pre>
      )}
    </div>
  );
}
