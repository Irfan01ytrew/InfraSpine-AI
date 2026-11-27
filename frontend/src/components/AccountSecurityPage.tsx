import { type FormEvent, useEffect, useState } from "react";
import { api, describeError } from "../api";
import { Spinner } from "../icons";
import type { User } from "../types";
import { Alert, PageHeader } from "./ui";

type MfaStatus = { enabled: boolean; recovery_codes_remaining: number; required_by_organization?: boolean };

export function AccountSecurityPage({ user, onMfaEnabled }: { user: User; onMfaEnabled?: () => void | Promise<void> }) {
  const [status, setStatus] = useState<MfaStatus | null>(null);
  const [enrollment, setEnrollment] = useState<{ secret: string; provisioning_uri: string } | null>(null);
  const [codes, setCodes] = useState<string[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function refresh() {
    setStatus(await api.mfaStatus());
  }

  async function acknowledgeRecoveryCodes() {
    try {
      await onMfaEnabled?.();
      setCodes(null);
    } catch (err) {
      setError(describeError(err));
    }
  }

  useEffect(() => { void refresh().catch((err) => setError(describeError(err))); }, []);

  async function submit(event: FormEvent<HTMLFormElement>, action: "start" | "confirm" | "rotate" | "disable") {
    event.preventDefault();
    const formElement = event.currentTarget;
    const form = new FormData(formElement);
    setBusy(true);
    setError(null);
    try {
      if (action === "start") {
        setEnrollment(await api.beginMfaEnrollment(String(form.get("password"))));
        setCodes(null);
      } else if (action === "confirm") {
        const result = await api.confirmMfaEnrollment(String(form.get("code")));
        setCodes(result.recovery_codes);
        setEnrollment(null);
        await refresh();
      } else if (action === "rotate") {
        const result = await api.rotateMfaRecoveryCodes(String(form.get("password")), String(form.get("code")));
        setCodes(result.recovery_codes);
        await refresh();
      } else {
        await api.disableMfa(String(form.get("password")), String(form.get("code")));
        setCodes(null);
        setEnrollment(null);
        await refresh();
      }
      formElement.reset();
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  }

  if (!status) return <div className="loading"><Spinner size={20} /> Loading account security…</div>;

  return <div className="account-security-page">
    <PageHeader
      title="Account security"
      description={<>Manage the second factor for {user.email}.</>}
    />
    <Alert tone="warning" className="mb-4">{status.required_by_organization || user.mfa_enrollment_required
      ? "Your organization requires MFA. Keep an authenticator and recovery codes available; this account cannot turn MFA off while the policy is active."
      : "MFA applies after enrollment for this account. Organization-wide enforcement and single sign-on depend on deployment identity policy."}</Alert>
    {error && <Alert tone="danger" className="mb-4">{error}</Alert>}
    {codes && <section className="card account-security-codes" aria-live="polite"><h2>Save these recovery codes</h2><p className="muted">Each code works once. This list will be hidden when you leave or refresh this page.</p><pre>{codes.join("\n")}</pre><button className="btn" onClick={() => void acknowledgeRecoveryCodes()}>{user.mfa_enrollment_required ? "I saved these recovery codes" : "Hide recovery codes"}</button></section>}
    {!status.enabled ? <section className="card account-security-card">
      <h2>Authenticator app</h2>
      <p className="muted">Use an authenticator app that supports standard time-based one-time passwords.</p>
      {!enrollment ? <form className="account-security-form" onSubmit={(event) => void submit(event, "start")}>
        <label className="field"><span className="field-label">Current password</span><input name="password" type="password" autoComplete="current-password" required /></label>
        <button className="btn btn-primary" disabled={busy}>{busy ? <Spinner size={14} /> : null} Begin setup</button>
      </form> : <form className="account-security-form" onSubmit={(event) => void submit(event, "confirm")}>
        <label className="field"><span className="field-label">Setup key</span><input value={enrollment.secret} readOnly aria-label="Authenticator setup key" /></label>
        <p className="muted small">You can also enter this provisioning address in your authenticator app:</p>
        <textarea value={enrollment.provisioning_uri} readOnly rows={3} aria-label="Authenticator provisioning address" />
        <label className="field"><span className="field-label">Current six-digit code</span><input name="code" inputMode="numeric" autoComplete="one-time-code" pattern="[0-9]{6}" minLength={6} maxLength={6} required /></label>
        <button className="btn btn-primary" disabled={busy}>{busy ? <Spinner size={14} /> : null} Confirm and enable MFA</button>
      </form>}
    </section> : <>
      <section className="card account-security-card"><h2>MFA is enabled</h2><p className="muted">{status.recovery_codes_remaining} unused recovery codes remain.</p>
        <form className="account-security-form" onSubmit={(event) => void submit(event, "rotate")}>
          <h3>Replace recovery codes</h3>
          <label className="field"><span className="field-label">Current password</span><input name="password" type="password" autoComplete="current-password" required /></label>
          <label className="field"><span className="field-label">Current authenticator code</span><input name="code" inputMode="numeric" autoComplete="one-time-code" pattern="[0-9]{6}" minLength={6} maxLength={6} required /></label>
          <button className="btn" disabled={busy}>Generate new recovery codes</button>
        </form>
      </section>
      {!status.required_by_organization && <section className="card account-security-card"><h2>Turn off MFA</h2><p className="muted">Confirm with your password and an authenticator or unused recovery code. Other signed-in sessions will be revoked.</p>
        <form className="account-security-form" onSubmit={(event) => void submit(event, "disable")}>
          <label className="field"><span className="field-label">Current password</span><input name="password" type="password" autoComplete="current-password" required /></label>
          <label className="field"><span className="field-label">Authenticator or recovery code</span><input name="code" autoComplete="one-time-code" required /></label>
          <button className="btn btn-danger" disabled={busy}>Turn off MFA</button>
        </form>
      </section>}
    </>}
  </div>;
}
