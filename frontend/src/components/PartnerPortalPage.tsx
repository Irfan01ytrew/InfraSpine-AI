import { type FormEvent, useEffect, useState } from "react";
import { ApiError, api, describeError } from "../api";
import { IconAlert, IconLogOut, IconLogo, Spinner } from "../icons";
import type { PartnerAnswer, PartnerIdentity } from "../types";

type PartnerExchange = PartnerAnswer & { question: string };
type PortalMode = "login" | "accept";

export function PartnerPortalPage() {
  const [identity, setIdentity] = useState<PartnerIdentity | null>(null);
  const [checking, setChecking] = useState(true);
  const [mode, setMode] = useState<PortalMode>(() => readPartnerToken() ? "accept" : "login");
  const [inviteToken, setInviteToken] = useState(() => readPartnerToken() ?? "");
  const [workspaceSlug, setWorkspaceSlug] = useState("");
  const [email, setEmail] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [question, setQuestion] = useState("");
  const [messages, setMessages] = useState<PartnerExchange[]>([]);

  useEffect(() => {
    api.partnerMe()
      .then(setIdentity)
      .catch((err: unknown) => {
        if (!(err instanceof ApiError) || err.status !== 401) setError(describeError(err));
      })
      .finally(() => setChecking(false));
  }, []);

  async function signIn(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const principal = await api.partnerLogin(
        String(form.get("workspace_slug")), String(form.get("email")), String(form.get("password")),
      );
      setIdentity(principal);
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  }

  async function acceptInvitation(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!inviteToken) {
      setError("This invitation link is missing its token. Ask your workspace administrator for a new link.");
      return;
    }
    const form = new FormData(event.currentTarget);
    const password = String(form.get("password"));
    if (password !== String(form.get("confirm_password"))) {
      setError("The passwords do not match.");
      return;
    }
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const accepted = await api.acceptPartnerInvitation(inviteToken, password);
      setWorkspaceSlug(accepted.workspace_slug);
      setEmail(accepted.email);
      clearInvitationFromUrl();
      setInviteToken("");
      setMode("login");
      try {
        setIdentity(await api.partnerLogin(accepted.workspace_slug, accepted.email, password));
      } catch (err) {
        setNotice("Your invitation was accepted. Sign in with your new password to continue.");
        setError(describeError(err));
      }
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  }

  async function signOut() {
    setBusy(true);
    setError(null);
    try {
      await api.partnerLogout();
      setIdentity(null);
      setMessages([]);
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  }

  async function ask(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const prompt = question.trim();
    if (!prompt) return;
    setBusy(true);
    setError(null);
    try {
      const answer = await api.askPartnerAssistant(prompt);
      setMessages((current) => [...current, { ...answer, question: prompt }]);
      setQuestion("");
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) setIdentity(null);
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  }

  if (checking) {
    return <div className="fullscreen-center"><Spinner size={22} /></div>;
  }

  return (
    <div className="partner-portal">
      <header className="partner-header">
        <a className="brand" href="#/partner" aria-label="Partner portal home">
          <span className="brand-mark"><IconLogo size={17} /></span>
          InfraSpine Partner
        </a>
        {identity && (
          <div className="partner-account">
            <span title={identity.email}>{identity.email}</span>
            <button className="btn btn-ghost" type="button" onClick={signOut} disabled={busy}>
              <IconLogOut size={15} /> Sign out
            </button>
          </div>
        )}
      </header>

      <main className="partner-main">
        {error && <div className="inline-alert inline-alert-error" role="alert"><IconAlert size={16} />{error}</div>}
        {notice && <div className="partner-notice" role="status">{notice}</div>}
        {identity ? (
          <section className="partner-workspace">
            <div className="partner-page-heading">
              <span className="partner-eyebrow">{identity.workspace_name}</span>
              <h1>Ask your workspace</h1>
              <p className="muted">Answers use the sources shared with this partner workspace and include citations when evidence is available.</p>
            </div>
            <div className="partner-conversation" aria-live="polite">
              {messages.length === 0 ? (
                <div className="partner-empty">
                  <span className="partner-empty-mark"><IconLogo size={20} /></span>
                  <h2>What would you like to know?</h2>
                  <p className="muted">Ask a question about the information your organization shared with you.</p>
                </div>
              ) : messages.map((item, index) => (
                <article className="partner-exchange" key={`${index}-${item.question}`}>
                  <div className="partner-question"><span>You</span><p>{item.question}</p></div>
                  <div className="partner-answer">
                    <div className="partner-answer-heading"><span>InfraSpine</span>
                      {item.insufficient_evidence && <span className="partner-evidence-state">Evidence not found</span>}
                    </div>
                    <p className="partner-answer-text">{item.answer}</p>
                    {item.citations.length > 0 && (
                      <section className="partner-citations" aria-label="Answer sources">
                        <h3>Sources</h3>
                        {item.citations.map((citation) => (
                          <article className="partner-citation" key={`${citation.source_record_id}-${citation.source_id}`}>
                            <strong><span className="partner-citation-id">{citation.source_id}</span> {citation.title}</strong>
                            {citation.location_label && <small>{citation.location_label}</small>}
                            <p>{citation.excerpt}</p>
                            {citation.download_available && <a href={`/api/partner/sources/${encodeURIComponent(citation.source_record_id)}/file`} download>Download original</a>}
                          </article>
                        ))}
                      </section>
                    )}
                  </div>
                </article>
              ))}
            </div>
            <form className="partner-composer" onSubmit={ask}>
              <label className="field-label" htmlFor="partner-question">Your question</label>
              <div className="partner-composer-row">
                <textarea id="partner-question" value={question} onChange={(event) => setQuestion(event.target.value)}
                  placeholder="Ask about the shared workspace sources…" maxLength={4000} required disabled={busy} />
                <button className="btn btn-primary" type="submit" disabled={busy || !question.trim()}>
                  {busy ? <Spinner /> : null}{busy ? "Thinking…" : "Ask"}
                </button>
              </div>
              <p className="muted small">Use answers as a guide and check the cited source for details.</p>
            </form>
          </section>
        ) : (
          <section className="partner-auth-card">
            <span className="partner-auth-mark"><IconLogo size={20} /></span>
            {mode === "accept" ? (
              <>
                <h1>Set up partner access</h1>
                <p className="muted">Create a password for the workspace invitation you received.</p>
                <form key="partner-invitation" className="partner-auth-form" onSubmit={acceptInvitation}>
                  <label className="field"><span className="field-label">New password</span>
                    <input name="password" type="password" minLength={12} maxLength={1024} autoComplete="new-password" required />
                  </label>
                  <label className="field"><span className="field-label">Confirm password</span>
                    <input name="confirm_password" type="password" minLength={12} maxLength={1024} autoComplete="new-password" required />
                  </label>
                  <button className="btn btn-primary btn-lg btn-block" type="submit" disabled={busy}>
                    {busy ? <Spinner /> : null}{busy ? "Setting up access…" : "Accept invitation"}
                  </button>
                </form>
                <button className="btn btn-ghost partner-switch" type="button" onClick={() => { setMode("login"); setError(null); }}>
                  Already have an account? Sign in
                </button>
              </>
            ) : (
              <>
                <h1>Partner sign in</h1>
                <p className="muted">Use the workspace and account details from your invitation.</p>
                <form key="partner-login" className="partner-auth-form" onSubmit={signIn}>
                  <label className="field"><span className="field-label">Workspace</span>
                    <input name="workspace_slug" value={workspaceSlug} onChange={(event) => setWorkspaceSlug(event.target.value)}
                      autoComplete="organization" placeholder="your-workspace" pattern="[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?" required />
                  </label>
                  <label className="field"><span className="field-label">Email</span>
                    <input name="email" type="email" value={email} onChange={(event) => setEmail(event.target.value)}
                      autoComplete="username" placeholder="you@example.com" required />
                  </label>
                  <label className="field"><span className="field-label">Password</span>
                    <input name="password" type="password" autoComplete="current-password" required />
                  </label>
                  <button className="btn btn-primary btn-lg btn-block" type="submit" disabled={busy}>
                    {busy ? <Spinner /> : null}{busy ? "Signing in…" : "Sign in"}
                  </button>
                </form>
                {inviteToken && <button className="btn btn-ghost partner-switch" type="button" onClick={() => { setMode("accept"); setError(null); }}>
                  Set up access from an invitation
                </button>}
              </>
            )}
          </section>
        )}
      </main>
      <footer className="partner-footer">Access is limited to the sources shared with this partner workspace.</footer>
    </div>
  );
}

function readPartnerToken(): string | null {
  const hash = window.location.hash;
  if (!hash.startsWith("#/partner")) return null;
  const queryStart = hash.indexOf("?");
  if (queryStart < 0) return null;
  return new URLSearchParams(hash.slice(queryStart + 1)).get("token");
}

function clearInvitationFromUrl() {
  window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}#/partner`);
}
