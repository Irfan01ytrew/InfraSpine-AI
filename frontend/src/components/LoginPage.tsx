import { type FormEvent, useState } from "react";
import { api, describeError } from "../api";
import { IconAlert, IconLogo, Spinner } from "../icons";
import type { User } from "../types";

export function LoginPage({ onSignedIn, brandName = "InfraSpine AI" }: { onSignedIn: (user: User) => void; brandName?: string }) {
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [challenge, setChallenge] = useState<{ token: string; email: string } | null>(null);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setBusy(true);
    setError(null);
    try {
      const result = await api.login(String(form.get("email")), String(form.get("password")));
      if ("mfa_required" in result) setChallenge({ token: result.challenge_token, email: String(form.get("email")) });
      else onSignedIn(result);
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  }

  async function verifyMfa(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!challenge) return;
    const form = new FormData(event.currentTarget);
    setBusy(true);
    setError(null);
    try {
      onSignedIn(await api.verifyMfaLogin(challenge.token, String(form.get("code"))));
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login">
      <section className="login-hero">
        <div className="brand">
          <span className="brand-mark">
            <IconLogo size={18} />
          </span>
          {brandName}
        </div>
        <div className="login-hero-body">
          <span className="login-eyebrow">Enterprise knowledge platform</span>
          <h1>Answers from your organization's documents, in seconds.</h1>
          <p>Ask in English or Urdu. Every answer links back to the page it came from.</p>
        </div>
        <p className="login-hero-foot">© {new Date().getFullYear()} {brandName}</p>
      </section>

      <section className="login-form-wrap">
        <form className="login-form" onSubmit={challenge ? verifyMfa : submit}>
          {challenge ? <>
            <h2>Verify your sign-in</h2>
            <p className="muted">Enter a current authenticator code or one of your saved recovery codes for {challenge.email}.</p>
            <label className="field">
              <span className="field-label">Authenticator or recovery code</span>
              <input name="code" type="text" inputMode="numeric" autoComplete="one-time-code" maxLength={64} required autoFocus />
            </label>
          </> : <>
            <h2>Welcome back</h2>
            <p className="muted">Sign in with your work account to continue.</p>
            <label className="field">
              <span className="field-label">Email</span>
              <input name="email" type="email" autoComplete="username" placeholder="you@company.com" required autoFocus />
            </label>
            <label className="field">
              <span className="field-label">Password</span>
              <input name="password" type="password" autoComplete="current-password" placeholder="••••••••" required />
            </label>
          </>}
          {error && (
            <div className="inline-alert inline-alert-error">
              <IconAlert size={16} />
              {error}
            </div>
          )}
          <button className="btn btn-primary btn-lg btn-block" disabled={busy}>
            {busy ? <Spinner /> : null}
            {busy ? (challenge ? "Verifying…" : "Signing in…") : (challenge ? "Verify and sign in" : "Sign in")}
          </button>
          {challenge && <button className="btn btn-block" type="button" disabled={busy} onClick={() => { setChallenge(null); setError(null); }}>Back to sign in</button>}
        </form>
      </section>
    </div>
  );
}
