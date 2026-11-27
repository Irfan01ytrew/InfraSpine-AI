import { useEffect, useRef, useState } from "react";
import { ApiError, api, describeError } from "../api";
import { formatDate, formatTokens, timeAgo } from "../format";
import {
  IconActivity,
  IconAlert,
  IconBrain,
  IconCheck,
  IconExternal,
  IconEye,
  IconEyeOff,
  IconKey,
  IconShield,
  IconTrash,
  IconX,
  IconZap,
  Spinner,
} from "../icons";
import type { ModelOption, ProviderMetadata, ProviderState, SettingField, Settings, TestConnectionResult } from "../types";
import { StatusPill } from "./AdminConsole";
import { useFeedback } from "./Feedback";
import { Card } from "./ui";

interface Props {
  metadata: ProviderMetadata;
  state: ProviderState;
  onChanged: () => Promise<void>;
}

/** What to try next for each verification failure the server reports. */
const TEST_HINTS: Record<string, string> = {
  invalid_credentials: "Check that the key is correct, not revoked, and belongs to this provider. Replace it under Saved API key.",
  model_unavailable: "Check the model ID spelling, or use one your key has access to.",
  timeout: "Check the API base URL and try again. The provider may be slow or down.",
  rate_limited: "Wait a minute and test again, or check the account's quota and billing.",
  provider_unavailable: "Check the API base URL (it usually ends in /v1) and that the provider is online.",
  unsafe_endpoint: "Use a public HTTPS API URL.",
  redirect_refused: "Enter the final API URL, not one that redirects.",
};

const KEY_LABELS: Record<TestConnectionResult["tested"], string> = {
  provided_key: "Entered key",
  pending_key: "Pending key",
  active_key: "Active key",
};

type TestOutcome =
  | { ok: true; result: TestConnectionResult }
  | { ok: false; result: TestConnectionResult | null; message: string };

/** Host case, default ports and trailing slashes don't make a different endpoint (the server compares the same way). */
function comparableUrl(raw: string): string {
  try {
    const url = new URL(raw.trim());
    return `${url.protocol}//${url.host.replace(/\.$/, "")}${url.pathname.replace(/\/+$/, "")}`;
  } catch {
    return raw.trim();
  }
}

/** Effective bounds for a setting; output-token limits depend on the selected model. */
function bounds(field: SettingField, model: ModelOption | undefined) {
  if (field.per_model_limit && model) {
    return { min: field.minimum ?? 1, max: model.max_output_tokens, def: model.default_output_tokens };
  }
  return { min: field.minimum ?? 0, max: field.maximum ?? 0, def: field.default };
}

export function ProviderPanel({ metadata, state, onChanged }: Props) {
  const { toast, confirm } = useFeedback();
  const [model, setModel] = useState(state.model);
  const [baseUrl, setBaseUrl] = useState(state.base_url ?? "");
  const [settings, setSettings] = useState<Settings>(state.settings);
  const [editingEndpoint, setEditingEndpoint] = useState(false);
  const [editingKey, setEditingKey] = useState(
    () => !Boolean(state.pending_credential || state.active_credential),
  );
  const [busy, setBusy] = useState<string | null>(null);
  const [revealKey, setRevealKey] = useState(false);
  const [testOutcome, setTestOutcome] = useState<TestOutcome | null>(null);
  // Uncontrolled inputs: a key is read once on submit and cleared, never kept in React state.
  const keyInput = useRef<HTMLInputElement>(null);
  const endpointKeyInput = useRef<HTMLInputElement>(null);

  // Re-sync when the server-side version changes (our save, or another admin's).
  // Keyed on version only, so a rejected save keeps the admin's unsaved edits.
  useEffect(() => {
    setModel(state.model);
    setBaseUrl(state.base_url ?? "");
    setSettings(state.settings);
    setEditingEndpoint(false);
    setEditingKey(!Boolean(state.pending_credential || state.active_credential));
    setTestOutcome(null);
  }, [state.version]);

  const isCustom = metadata.id.startsWith("custom-");
  const selectedModel = metadata.models.find((m) => m.id === model);
  const savedModelRetired = !isCustom && state.configured && !metadata.models.some((m) => m.id === state.model);
  // A changed API URL needs its own key: saved keys are never sent to a different endpoint.
  const endpointChanged = isCustom && comparableUrl(baseUrl) !== comparableUrl(state.base_url ?? "");
  const dirty =
    model !== state.model ||
    (isCustom && baseUrl !== (state.base_url ?? "")) ||
    JSON.stringify(settings) !== JSON.stringify(state.settings);
  const hasKey = Boolean(state.pending_credential || state.active_credential);
  const canActivate = Boolean(state.pending_credential || (state.active_credential && !state.is_active));
  const rotating = Boolean(state.pending_credential && state.active_credential);

  async function run(label: string, action: () => Promise<{ title: string; body?: string }>) {
    setBusy(label);
    try {
      const { title, body } = await action();
      toast("success", title, body);
    } catch (err) {
      const conflict = err instanceof ApiError && err.status === 409;
      toast("error", conflict ? "Configuration changed" : "Action failed", conflict ? `${err.message} The latest version has been loaded.` : describeError(err));
    } finally {
      setBusy(null);
      await onChanged();
    }
  }

  function chooseModel(next: ModelOption) {
    const previous = selectedModel;
    setModel(next.id);
    // Keep token settings valid for the new model: adopt its default if the old value
    // was the old model's default or exceeds the new model's limit.
    setSettings((current) => {
      const updated = { ...current };
      for (const field of metadata.settings.filter((f) => f.per_model_limit)) {
        const value = Number(current[field.key]);
        if (value > next.max_output_tokens || (previous && value === previous.default_output_tokens)) {
          updated[field.key] = next.default_output_tokens;
        }
      }
      return updated;
    });
  }

  function save() {
    let endpointKey: string | undefined;
    if (endpointChanged) {
      endpointKey = endpointKeyInput.current?.value.trim() ?? "";
      if (endpointKeyInput.current) endpointKeyInput.current.value = "";
      if (!endpointKey) {
        toast("error", "API key required", "Enter the API key for the new API URL. Saved keys are never sent to a different URL.");
        return;
      }
    }
    void run("save", async () => {
      await api.updateProvider(
        metadata.id,
        model.trim(),
        settings,
        state.version,
        isCustom ? baseUrl.trim() : undefined,
        endpointKey,
      );
      setEditingEndpoint(false);
      return {
        title: "Configuration saved",
        body: endpointChanged
          ? "The new API URL was verified with the key you entered. Keys saved for the previous URL were deleted."
          : isCustom
            ? "The model was checked with your saved key."
            : "New requests use these settings immediately.",
      };
    });
  }

  function takeKey(): string {
    const value = keyInput.current?.value.trim() ?? "";
    if (keyInput.current) keyInput.current.value = "";
    setRevealKey(false);
    return value;
  }

  function testConnection() {
    // The endpoint key is left in its field so the edit can still be saved after the test.
    const key = takeKey() || (endpointChanged ? endpointKeyInput.current?.value.trim() ?? "" : "");
    if (endpointChanged && !key) {
      toast("error", "API key required", "Enter the API key to test a new API URL. Saved keys are only sent to their own URL.");
      return;
    }
    setBusy("test");
    setTestOutcome(null);
    void (async () => {
      try {
        const result = await api.testConnection(metadata.id, key || null, model.trim(), isCustom ? baseUrl.trim() : undefined);
        setTestOutcome(result.ok ? { ok: true, result } : { ok: false, result, message: result.message });
      } catch (err) {
        setTestOutcome({ ok: false, result: null, message: describeError(err) });
      } finally {
        setBusy(null);
      }
    })();
  }

  function stageKey() {
    const key = takeKey();
    if (!key) {
      toast("error", "API key required", "Paste an API key first.");
      return;
    }
    void run("stage", async () => {
      await api.stageCredential(metadata.id, key, state.version);
      return state.active_credential
        ? { title: "New key verified", body: "Activate it to rotate. The current key keeps serving until then." }
        : { title: "Key verified and encrypted", body: "Activate the provider to start using it." };
    });
  }

  async function activate() {
    const ok = await confirm({
      title: rotating ? "Rotate API key?" : `Activate ${metadata.display_name}?`,
      body: rotating
        ? "The pending key is verified again, then replaces the current key. The current key is retired and permanently deleted."
        : `The key is verified again, then ${metadata.display_name} serves all AI requests. Any other active provider is deactivated.`,
      confirmLabel: rotating ? "Rotate key" : "Activate",
    });
    if (!ok) return;
    void run("activate", async () => {
      await api.activate(metadata.id, state.version);
      return rotating
        ? { title: "Key rotated", body: "New requests use the new key." }
        : { title: `${metadata.display_name} is live`, body: "AI requests now use this provider." };
    });
  }

  async function deactivate() {
    const ok = await confirm({
      title: `Deactivate ${metadata.display_name}?`,
      body: "AI chat will be unavailable until a provider is activated again. Stored keys are kept.",
      confirmLabel: "Deactivate",
      tone: "danger",
    });
    if (!ok) return;
    void run("deactivate", async () => {
      await api.deactivate(metadata.id, state.version);
      return { title: `${metadata.display_name} deactivated` };
    });
  }

  async function setImageSharing(authorized: boolean) {
    const ok = await confirm({
      title: authorized ? `Send document images to ${metadata.display_name}?` : "Stop sending document images?",
      body: authorized
        ? `Pictures in uploaded PDFs and uploaded images are sent to ${metadata.display_name} to be described, so questions can be answered from them.`
        : "New documents are indexed from their text only. Uploaded images can no longer be indexed.",
      confirmLabel: authorized ? "Allow" : "Stop",
      tone: authorized ? undefined : "danger",
    });
    if (!ok) return;
    void run("images", async () => {
      await api.setImageSharing(metadata.id, authorized, state.version);
      return { title: authorized ? "Document images allowed" : "Document images stopped" };
    });
  }

  async function remove() {
    const ok = await confirm({
      title: `Remove ${metadata.display_name}?`,
      body: isCustom
        ? "This provider, its settings and its stored API keys are permanently deleted. Users can no longer pick it in chat. Past answers keep their model label."
        : "Its settings and stored API keys are permanently deleted, and it returns to \"Not configured\". You can set it up again later.",
      confirmLabel: "Remove",
      tone: "danger",
    });
    if (!ok) return;
    void run("remove", async () => {
      await api.removeProvider(metadata.id, state.version);
      return { title: `${metadata.display_name} removed` };
    });
  }

  const steps = [
    { label: "Choose model", done: state.configured },
    { label: "Verify API key", done: hasKey },
    { label: "Activate", done: state.is_active },
  ];

  return (
    <div className="stack">
      {/* ------------------------------------------------------------ overview */}
      <Card className="p-5">
        <div className="provider-head">
          <div className="provider-logo">{metadata.display_name.charAt(0)}</div>
          <div className="provider-title">
            <div className="provider-title-row">
              <h2>{metadata.display_name}</h2>
              <StatusPill state={state} />
            </div>
            <p className="muted">
              {metadata.description}{" "}
              {metadata.docs_url && (
                <a className="link" href={metadata.docs_url} target="_blank" rel="noreferrer noopener">
                  Docs <IconExternal size={12} />
                </a>
              )}
            </p>
          </div>
          <div className="provider-actions">
            {state.configured && !state.is_active && (
              <button className="btn btn-ghost" onClick={remove} disabled={busy !== null} title="Delete this provider and its keys">
                {busy === "remove" ? <Spinner /> : <IconTrash size={14} />} Remove
              </button>
            )}
            {hasKey && (
              <button className="btn" onClick={testConnection} disabled={busy !== null}>
                {busy === "test" ? <Spinner /> : <IconActivity size={16} />} Test connection
              </button>
            )}
            {state.is_active ? (
              <button className="btn" onClick={deactivate} disabled={busy !== null}>
                {busy === "deactivate" && <Spinner />}
                Deactivate
              </button>
            ) : (
              <button className="btn btn-primary" onClick={activate} disabled={!canActivate || busy !== null}>
                {busy === "activate" ? <Spinner /> : <IconZap size={16} />}
                {busy === "activate" ? "Verifying…" : "Activate"}
              </button>
            )}
            {state.is_active && rotating && (
              <button className="btn btn-primary" onClick={activate} disabled={busy !== null}>
                {busy === "activate" ? <Spinner /> : <IconZap size={16} />}
                {busy === "activate" ? "Verifying…" : "Rotate key"}
              </button>
            )}
          </div>
        </div>

        <ol className="stepper">
          {steps.map((step, i) => (
            <li key={step.label} className={step.done ? "step step-done" : "step"}>
              <span className="step-index">{step.done ? <IconCheck size={14} /> : i + 1}</span>
              {step.label}
            </li>
          ))}
        </ol>

        {testOutcome && <TestResult outcome={testOutcome} onDismiss={() => setTestOutcome(null)} />}

        <div className="stats">
          <Stat label="Model" value={state.configured ? (metadata.models.find((m) => m.id === state.model)?.label ?? state.model) : "—"} />
          <Stat
            label="Active key"
            value={state.active_credential ? <code>••••{state.active_credential.key_hint}</code> : "—"}
            hint={state.active_credential?.activated_at ? `since ${formatDate(state.active_credential.activated_at)}` : undefined}
          />
          <Stat
            label="Pending key"
            value={state.pending_credential ? <code>••••{state.pending_credential.key_hint}</code> : "—"}
            hint={state.pending_credential ? "verified, ready to activate" : undefined}
          />
          <Stat label="Last change" value={state.updated_at ? timeAgo(state.updated_at) : "—"} hint={state.updated_at ? formatDate(state.updated_at) : undefined} />
        </div>
      </Card>

      {/* ---------------------------------------------------- model & settings */}
      <Card className="p-5">
        <div className="card-title">
          <div>
            <h3>Model</h3>
            <p className="muted">
              {isCustom
                ? "Model changes are checked with your saved key. A new API URL needs the API key for that URL."
                : "Limits shown are the provider's maximums for each model."}
            </p>
          </div>
          {isCustom && !editingEndpoint && (
            <button className="btn btn-sm" type="button" onClick={() => setEditingEndpoint(true)}>
              Edit
            </button>
          )}
        </div>
        {savedModelRetired && (
          <div className="inline-alert inline-alert-warning">
            <IconAlert size={16} />
            The saved model <code>{state.model}</code> is no longer offered. Choose a model below and save.
          </div>
        )}
        {isCustom && editingEndpoint ? (
          <div className="custom-provider-fields">
            <label className="field">
              <span className="field-label">Model ID</span>
              <input
                type="text"
                value={model}
                maxLength={100}
                onChange={(event) => setModel(event.target.value)}
                disabled={busy !== null}
              />
            </label>
            <label className="field">
              <span className="field-label">API base URL</span>
              <input
                type="url"
                value={baseUrl}
                maxLength={500}
                onChange={(event) => setBaseUrl(event.target.value)}
                disabled={busy !== null}
              />
            </label>
            {endpointChanged && (
              <label className="field">
                <span className="field-label">API key for the new URL</span>
                <input
                  ref={endpointKeyInput}
                  type="password"
                  autoComplete="off"
                  spellCheck={false}
                  placeholder="Paste the API key for this URL"
                  disabled={busy !== null}
                />
              </label>
            )}
          </div>
        ) : isCustom ? (
          <div className="custom-provider-summary">
            <div>
              <span className="stat-label">Model ID</span>
              <code className="model-card-id">{state.model}</code>
            </div>
            <div>
              <span className="stat-label">API base URL</span>
              <code className="model-card-id">{state.base_url}</code>
            </div>
          </div>
        ) : (
          <div className="model-grid" role="radiogroup" aria-label="Model">
            {metadata.models.map((m) => (
              <button
                key={m.id}
                type="button"
                role="radio"
                aria-checked={m.id === model}
                className={`model-card ${m.id === model ? "model-card-selected" : ""}`}
                onClick={() => chooseModel(m)}
              >
                <span className="model-card-check">{m.id === model && <IconCheck size={14} />}</span>
                <span className="model-card-name">{m.label}</span>
                <code className="model-card-id">{m.id}</code>
                <span className="model-card-desc">{m.description}</span>
                {(m.context_window > 0 || m.max_output_tokens > 0) && (
                  <span className="chips">
                    {m.context_window > 0 && <span className="chip">{formatTokens(m.context_window)} context</span>}
                    {m.max_output_tokens > 0 && <span className="chip">{formatTokens(m.max_output_tokens)} max output</span>}
                  </span>
                )}
              </button>
            ))}
          </div>
        )}

        <div className="divider" />

        <div className="card-title">
          <h3>Parameters</h3>
        </div>
        <div className="params">
          {metadata.settings.map((field) => (
            <SettingControl
              key={field.key}
              field={field}
              model={selectedModel}
              value={settings[field.key] ?? bounds(field, selectedModel).def}
              onChange={(value) => setSettings({ ...settings, [field.key]: value })}
            />
          ))}
        </div>

        {(dirty || !state.configured || (isCustom && editingEndpoint)) && (
          <div className="savebar">
            <span className="muted">
              {endpointChanged
                ? "The new URL is verified with the key you enter, which then replaces the saved keys."
                : isCustom && editingEndpoint
                  ? "Save the edit after it has been verified. The saved API key stays in place."
                  : state.configured
                    ? "You have unsaved changes."
                    : "Save to create this provider's configuration."}
              {state.is_active && !endpointChanged && model !== state.model &&
                " The new model is verified with the active key first."}
            </span>
            <div className="savebar-actions">
              {state.configured && (
                <button
                  className="btn btn-ghost"
                  onClick={() => {
                    setModel(state.model);
                    setBaseUrl(state.base_url ?? "");
                    setSettings(state.settings);
                    setEditingEndpoint(false);
                  }}
                  disabled={busy !== null}
                >
                  {isCustom && editingEndpoint ? "Cancel" : "Discard"}
                </button>
              )}
              <button
                className="btn btn-primary"
                onClick={save}
                // A provider that was never configured can be saved with its preselected default model.
                disabled={busy !== null || (state.configured && !dirty) || (!isCustom && !selectedModel)}
              >
                {busy === "save" && <Spinner />}
                {state.configured ? "Save changes" : "Save configuration"}
              </button>
            </div>
          </div>
        )}
      </Card>

      {/* ------------------------------------------------------- document images */}
      {state.configured && (
        <Card className="p-5">
          <div className="card-title">
            <div>
              <h3>Pictures inside documents</h3>
              <p className="muted">
                {state.image_sharing_authorized
                  ? `This provider may describe charts and pictures from your documents (allowed${state.image_sharing_authorized_at ? ` since ${formatDate(state.image_sharing_authorized_at)}` : ""}).`
                  : "Pictures from your documents are not sent to this provider. Allow it to let answers use charts and diagrams."}
              </p>
            </div>
            <button
              className={state.image_sharing_authorized ? "btn btn-sm" : "btn btn-sm btn-primary"}
              type="button"
              onClick={() => void setImageSharing(!state.image_sharing_authorized)}
              disabled={busy !== null}
            >
              {busy === "images" && <Spinner />}
              {state.image_sharing_authorized ? "Stop sending pictures" : "Allow pictures"}
            </button>
          </div>
        </Card>
      )}

      {/* -------------------------------------------------------------- api key */}
      <Card className="p-5">
        <div className="card-title">
          <div>
            <h3>{hasKey ? "Saved API key" : "API key"}</h3>
            <p className="muted">
              {hasKey
                ? "Your saved key is reused when you edit the model. It is only ever sent to the API URL it was verified with."
                : `Paste a ${metadata.display_name} API key. It is tested live before it is stored.`}
            </p>
          </div>
          {hasKey && !editingKey && (
            <button className="btn btn-sm" type="button" onClick={() => setEditingKey(true)} disabled={busy !== null}>
              Replace API key
            </button>
          )}
        </div>
        {hasKey && !editingKey ? (
          <div className="saved-key-summary">
            <span className="saved-key-badge"><IconKey size={16} /> Key saved securely</span>
            <span className="muted">
              {state.pending_credential && state.active_credential
                ? `Pending key ••••${state.pending_credential.key_hint} is ready to rotate.`
                : state.active_credential
                  ? `Active key ••••${state.active_credential.key_hint}`
                  : `Verified key ••••${state.pending_credential?.key_hint}`}
            </span>
          </div>
        ) : (
          <>
            <div className="key-row">
              <div className="input-with-icon">
                <IconKey size={16} />
                <input
                  ref={keyInput}
                  type={revealKey ? "text" : "password"}
                  autoComplete="off"
                  spellCheck={false}
                  placeholder={state.configured ? "Paste a new API key" : "Save the model first"}
                  disabled={!state.configured || busy !== null}
                  aria-label="API key"
                />
                <button type="button" className="icon-btn" onClick={() => setRevealKey(!revealKey)} aria-label={revealKey ? "Hide key" : "Show key"} disabled={!state.configured}>
                  {revealKey ? <IconEyeOff size={16} /> : <IconEye size={16} />}
                </button>
              </div>
              <button className="btn" type="button" onClick={testConnection} disabled={!state.configured || busy !== null}>
                {busy === "test" && <Spinner />}
                Test connection
              </button>
              <button className="btn btn-primary" type="button" onClick={stageKey} disabled={!state.configured || busy !== null}>
                {busy === "stage" && <Spinner />}
                Verify &amp; save
              </button>
              {hasKey && (
                <button className="btn btn-ghost" type="button" onClick={() => setEditingKey(false)} disabled={busy !== null}>
                  Cancel
                </button>
              )}
            </div>
          </>
        )}
        <p className="security-note">
          <IconShield size={15} />
          Encrypted with AES-256-GCM on the server. Stored keys are never shown again, only their last 4 characters.
          {hasKey && editingKey ? " Leave the field empty to test with the saved key." : ""}
        </p>
      </Card>
    </div>
  );
}

function TestResult({ outcome, onDismiss }: { outcome: TestOutcome; onDismiss: () => void }) {
  const result = outcome.result;
  const subject = result ? `${KEY_LABELS[result.tested]} · ${result.model} · ${result.latency_ms} ms` : null;
  const hint = !outcome.ok && result?.reason ? TEST_HINTS[result.reason] : undefined;
  return (
    <div className={`inline-alert test-result ${outcome.ok ? "inline-alert-success" : "inline-alert-error"}`} role="status">
      {outcome.ok ? <IconCheck size={16} /> : <IconAlert size={16} />}
      <div className="test-result-body">
        <strong>{outcome.ok ? "Connection verified" : "Connection failed"}</strong>
        <span>{outcome.ok ? "The key is valid and the model is available." : outcome.message}</span>
        {result?.detail && <code className="test-result-detail">{result.detail}</code>}
        {hint && <span>{hint}</span>}
        {subject && <span className="test-result-meta">{subject}</span>}
      </div>
      <button type="button" className="icon-btn" onClick={onDismiss} aria-label="Dismiss">
        <IconX size={14} />
      </button>
    </div>
  );
}

function Stat({ label, value, hint }: { label: string; value: React.ReactNode; hint?: string }) {
  return (
    <div className="stat">
      <span className="stat-label">{label}</span>
      <span className="stat-value">{value}</span>
      {hint && <span className="stat-hint">{hint}</span>}
    </div>
  );
}

function SettingControl({
  field,
  model,
  value,
  onChange,
}: {
  field: SettingField;
  model: ModelOption | undefined;
  value: number | string;
  onChange: (value: number | string) => void;
}) {
  const { min, max } = bounds(field, model);

  if (field.type === "select") {
    return (
      <div className="param">
        <div className="param-head">
          <span className="field-label">
            {field.key === "thinking" && <IconBrain size={15} />} {field.label}
          </span>
        </div>
        <div className="segmented" role="radiogroup" aria-label={field.label}>
          {field.options.map((o) => (
            <button
              key={o.value}
              type="button"
              role="radio"
              aria-checked={value === o.value}
              className={value === o.value ? "segment segment-active" : "segment"}
              onClick={() => onChange(o.value)}
            >
              {o.label}
            </button>
          ))}
        </div>
        <p className="param-desc">{field.description}</p>
      </div>
    );
  }

  const numeric = Number(value);
  const outOfRange = numeric < min || numeric > max;

  if (field.per_model_limit) {
    // Token budgets span 256 → 384K, so offer presets plus an exact number input.
    const presets = [4096, 8192, 32768, 65536, 131072].filter((p) => p >= min && p < max);
    return (
      <div className="param param-wide">
        <div className="param-head">
          <span className="field-label">{field.label}</span>
          <span className="param-value">{Number.isFinite(numeric) ? numeric.toLocaleString() : "—"} tokens</span>
        </div>
        <div className="token-row">
          <div className="chips">
            {presets.map((p) => (
              <button key={p} type="button" className={`chip chip-button ${numeric === p ? "chip-active" : ""}`} onClick={() => onChange(p)}>
                {formatTokens(p)}
              </button>
            ))}
            <button type="button" className={`chip chip-button ${numeric === max ? "chip-active" : ""}`} onClick={() => onChange(max)}>
              Max · {formatTokens(max)}
            </button>
          </div>
          <input
            className={`token-input ${outOfRange ? "input-invalid" : ""}`}
            type="number"
            min={min}
            max={max}
            step={1}
            value={Number.isFinite(numeric) ? numeric : ""}
            onChange={(e) => onChange(Number(e.target.value))}
            aria-label={field.label}
          />
        </div>
        <p className={`param-desc ${outOfRange ? "text-error" : ""}`}>
          {outOfRange ? `Must be between ${min.toLocaleString()} and ${max.toLocaleString()} for this model. ` : ""}
          {field.description}
        </p>
      </div>
    );
  }

  const percent = max > min ? ((numeric - min) / (max - min)) * 100 : 0;
  return (
    <div className="param">
      <div className="param-head">
        <span className="field-label">{field.label}</span>
        <span className="param-value">{field.step !== null && field.step < 1 ? numeric.toFixed(1) : numeric}</span>
      </div>
      <input
        className="slider"
        type="range"
        min={min}
        max={max}
        step={field.step ?? 0.1}
        value={numeric}
        onChange={(e) => onChange(Number(e.target.value))}
        style={{ "--fill": `${percent}%` } as React.CSSProperties}
        aria-label={field.label}
      />
      <div className="slider-scale">
        <span>Precise</span>
        <span>Creative</span>
      </div>
      <p className="param-desc">{field.description}</p>
    </div>
  );
}
