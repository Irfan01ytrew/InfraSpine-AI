import { useEffect, useState } from "react";
import { api, describeError } from "../api";
import { formatDate } from "../format";
import type { MarketResearchReport, MarketResearchStatus } from "../types";
import { useFeedback } from "./Feedback";
import { Markdown } from "./Markdown";
import { Spinner } from "../icons";
import { Alert, Button, PageHeader } from "./ui";

export function MarketResearchPage({ isAdmin = false }: { isAdmin?: boolean }) {
  const { toast } = useFeedback();
  const [topic, setTopic] = useState("");
  const [consent, setConsent] = useState(false);
  const [status, setStatus] = useState<MarketResearchStatus | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [report, setReport] = useState<MarketResearchReport | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let active = true;
    api.marketResearchStatus()
      .then((value) => { if (active) setStatus(value); })
      .catch((error) => { if (active) setStatusError(describeError(error)); });
    return () => { active = false; };
  }, []);

  async function run(event: React.FormEvent) {
    event.preventDefault();
    const cleanTopic = topic.trim();
    if (cleanTopic.length < 4 || !consent || busy || !status?.enabled || !status.web_search_available) return;
    setBusy(true);
    setReport(null);
    try {
      setReport(await api.marketResearch(cleanTopic));
    } catch (error) {
      toast("error", "Market research could not run", describeError(error));
    } finally {
      setBusy(false);
    }
  }

  const ready = Boolean(status?.enabled && status.web_search_available);
  return (
    <>
      <PageHeader
        title={<>Market & competitor research</>}
        description="Analyze public market and competitor information with cited web sources. Internal company documents are not used."
      />
      <section className="card research-form">
        {status && !ready && status.enabled && isAdmin ? (
          <Alert tone="warning" className="mb-4"><div>
              <strong>Connect an AI provider to turn on web research.</strong>
              <ol className="mt-1 ml-4 list-decimal text-[13px] leading-relaxed">
                <li>Open AI providers and configure a model that can synthesize research results.</li>
                <li>Public search uses Gemini grounding when available, with Yahoo or Bing and DuckDuckGo fallbacks.</li>
              </ol>
              <Button size="sm" variant="primary" className="mt-2" onClick={() => { window.location.hash = "/providers"; }}>
                Configure AI provider
              </Button>
            </div></Alert>
        ) : status && !ready && status.enabled ? (
          <Alert tone="warning" className="mb-4">Web research is not available yet. Ask your administrator to configure an AI provider in AI providers.</Alert>
        ) : status ? (
          <Alert tone={ready ? "success" : "warning"} className="mb-4">{status.message}</Alert>
        ) : statusError ? (
          <Alert tone="danger" className="mb-4">Research availability could not be checked: {statusError}</Alert>
        ) : (
          <div className="research-progress" aria-live="polite"><Spinner size={16} /> Checking research availability…</div>
        )}
        <form onSubmit={(event) => void run(event)}>
          <label className="field">
            <span className="field-label">Market or competitor question</span>
            <textarea
              value={topic}
              onChange={(event) => setTopic(event.target.value)}
              maxLength={3000}
              minLength={4}
              rows={4}
              placeholder="Compare public cloud infrastructure offerings for mid-sized healthcare providers, with recent pricing and security developments."
              disabled={!ready || busy}
            />
          </label>
          <label className="research-consent">
            <input type="checkbox" checked={consent} onChange={(event) => setConsent(event.target.checked)} disabled={!ready || busy} />
            <span>I confirm this topic may be sent to the configured AI provider and public search services. This may incur provider charges. No internal documents or company profile are sent.</span>
          </label>
          <div className="research-form-foot">
            <span className="muted small">Reports are based on public web results and may require human review. Citations link to the sources used.</span>
            <button className="btn btn-primary" disabled={!ready || busy || !consent || topic.trim().length < 4}>
              {busy && <Spinner size={14} />} Run market research
            </button>
          </div>
        </form>
      </section>
      {busy && <section className="card research-progress" aria-live="polite"><Spinner size={18} /><span>Searching public web sources and preparing a cited report…</span></section>}
      {report && <section className="research-results">
        <div className="research-report-head">
          <div><h2>Market research report</h2><p className="muted">{report.topic} · {formatDate(report.generated_at)} · {report.provider} {report.model}</p></div>
          <button className="btn" onClick={() => window.print()}>Print report</button>
        </div>
        <article className="card research-finding">
          <div className="research-answer">
            <Markdown text={report.answer} renderCitation={(tag, key) => {
              const match = tag.match(/^S(\d+)$/);
              const source = match ? report.sources[Number(match[1]) - 1] : undefined;
              return source
                ? <a className="source-tag" key={key} href={source.url} target="_blank" rel="noopener noreferrer" title={source.title}>{tag}</a>
                : <span className="source-tag" key={key}>{tag}</span>;
            }} />
          </div>
          {report.sources.length > 0 && <div className="research-sources"><strong>Web sources</strong>{report.sources.map((source, index) => (
            <div key={source.url} className="research-source"><span className="source-tag">S{index + 1}</span><span><a href={source.url} target="_blank" rel="noopener noreferrer">{source.title}</a><small>{source.url}</small></span></div>
          ))}</div>}
        </article>
      </section>}
    </>
  );
}
