import { useEffect, useRef, useState, type FormEvent } from "react";
import { api, describeError } from "../api";
import { useFeedback } from "./Feedback";
import { PlugZap, ShieldCheck, Sparkles, X } from "lucide-react";
import { Button, Card, CardBody, CardHeader, Field, Input } from "./ui";

interface Props {
  onCancel: () => void;
  onCreated: (providerId: string) => Promise<void>;
  /** Pre-fill a known provider, e.g. when opened from Market research. */
  preset?: "gemini";
}

// Gemini's OpenAI-compatible endpoint; the same origin also enables grounded public web research.
const PRESETS = {
  gemini: { display_name: "Google Gemini", model: "gemini-2.5-flash", base_url: "https://generativelanguage.googleapis.com/v1beta/openai/" },
} as const;

export function AddProviderForm({ onCancel, onCreated, preset }: Props) {
  const { toast } = useFeedback();
  const [busy, setBusy] = useState(false);
  const [presetApplied, setPresetApplied] = useState<keyof typeof PRESETS | null>(null);
  const keyInput = useRef<HTMLInputElement>(null);
  const form = useRef<HTMLFormElement>(null);

  function applyPreset(name: keyof typeof PRESETS) {
    const values = PRESETS[name];
    for (const [field, value] of Object.entries(values)) {
      const input = form.current?.elements.namedItem(field) as HTMLInputElement | null;
      if (input) input.value = value;
    }
    setPresetApplied(name);
    keyInput.current?.focus();
  }

  useEffect(() => { if (preset) applyPreset(preset); }, [preset]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    const apiKey = String(data.get("api_key") ?? "").trim();
    if (!apiKey) {
      toast("error", "API key required", "Paste an API key first.");
      return;
    }

    setBusy(true);
    if (keyInput.current) keyInput.current.value = "";
    try {
      const provider = await api.createCustomProvider({
        display_name: String(data.get("display_name") ?? "").trim(),
        base_url: String(data.get("base_url") ?? "").trim(),
        model: String(data.get("model") ?? "").trim(),
        api_key: apiKey,
      });
      toast("success", "Provider added", "The key was verified and saved inactive. Your current provider is still serving requests.");
      await onCreated(provider.provider);
    } catch (error) {
      toast("error", "Could not add provider", describeError(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card className="mb-5 overflow-hidden">
      <CardHeader
        icon={<PlugZap />}
        title="Add an AI provider"
        description="Connect any OpenAI-compatible service (OpenAI, Gemini, Azure, a local model server). The key is checked first and saved inactive; your current provider keeps running until you activate the new one."
        actions={<Button size="sm" variant="ghost" onClick={onCancel} disabled={busy}><X /> Cancel</Button>}
      />
      <CardBody>
        <form ref={form} className="grid gap-4" onSubmit={(event) => void submit(event)}>
          <div className="flex flex-wrap items-center gap-2 rounded-lg border border-border bg-surface-2/50 px-3 py-2">
            <Sparkles className="size-4 text-primary" />
            <span className="text-xs text-foreground-2">Quick setup:</span>
            <Button size="sm" onClick={() => applyPreset("gemini")} disabled={busy}>Use Google Gemini</Button>
            {presetApplied === "gemini" && <span className="text-xs text-muted">Now paste a Gemini key from <a className="text-primary underline" href="https://aistudio.google.com/apikey" target="_blank" rel="noopener noreferrer">Google AI Studio</a>. Gemini also turns on Market research web search.</span>}
          </div>
          <div className="grid gap-4 md:grid-cols-2">
            <Field label="Provider name"><Input name="display_name" maxLength={80} placeholder="e.g. OpenAI" required disabled={busy} /></Field>
            <Field label="Model ID"><Input name="model" maxLength={100} placeholder="e.g. gpt-4o" required disabled={busy} /></Field>
            <Field label="API base URL" className="md:col-span-2" hint="Remote addresses must use HTTPS. Redirects are not followed."><Input name="base_url" type="url" maxLength={500} placeholder="https://api.example.com/v1" required disabled={busy} /></Field>
            <Field label="API key" className="md:col-span-2" hint="Encrypted on the server; only the last 4 characters are ever shown again."><Input ref={keyInput} name="api_key" type="password" autoComplete="new-password" spellCheck={false} placeholder="Paste the API key" required disabled={busy} /></Field>
          </div>
          <div><Button type="submit" variant="primary" loading={busy} disabled={busy}>{!busy && <ShieldCheck />} {busy ? "Verifying…" : "Verify and add (inactive)"}</Button></div>
        </form>
      </CardBody>
    </Card>
  );
}
