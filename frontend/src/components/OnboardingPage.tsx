import { useState } from "react";
import { api } from "../api";
import { IconLogo, Spinner } from "../icons";

export function OnboardingPage({ token, onAccepted }: { token: string; onAccepted: () => void }) {
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (password !== confirmation) return setError("The passwords do not match.");
    if (password.length < 8) return setError("Use at least 8 characters.");
    setBusy(true);
    setError(null);
    try {
      const result = await api.acceptInvitation(token, password);
      setMessage(result.message);
      setPassword("");
      setConfirmation("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "This invitation could not be accepted.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="onboarding-shell">
      <section className="card onboarding-card">
        <div className="brand onboarding-brand"><span className="brand-mark"><IconLogo size={18} /></span>InfraSpine AI</div>
        {message ? (
          <>
            <h1>Account ready</h1>
            <p className="muted">{message}</p>
            <button className="btn btn-primary" onClick={onAccepted}>Go to sign in</button>
          </>
        ) : (
          <form onSubmit={(event) => void submit(event)}>
            <h1>Set up your account</h1>
            <p className="muted">Create a password to accept your InfraSpine invitation.</p>
            <label className="field">
              <span className="field-label">Password</span>
              <input type="password" autoComplete="new-password" minLength={8} maxLength={256} required value={password} onChange={(event) => setPassword(event.target.value)} />
            </label>
            <label className="field">
              <span className="field-label">Confirm password</span>
              <input type="password" autoComplete="new-password" minLength={8} maxLength={256} required value={confirmation} onChange={(event) => setConfirmation(event.target.value)} />
            </label>
            {error && <div className="banner banner-error" role="alert">{error}</div>}
            <button className="btn btn-primary onboarding-submit" type="submit" disabled={busy}>
              {busy && <Spinner size={14} />} Accept invitation
            </button>
          </form>
        )}
      </section>
    </main>
  );
}
