import { useEffect, useState } from "react";
import { Languages } from "lucide-react";
import { api, describeError } from "../api";
import { Spinner } from "../icons";
import { LANGUAGES, isLanguage, setLanguage } from "../i18n.mjs";
import { Alert, Button, Card, CardBody, CardHeader, Field } from "./ui";

/**
 * Per-user language (Step Z slice 4): the language replies and navigation
 * are shown in. English, Roman Urdu, or Urdu script. Choosing Urdu script
 * also switches the page to a right-to-left layout.
 *
 * This only ever touches the caller's own preference row (GET/PUT
 * /api/language/preference); it never changes authorization, permissions,
 * approvals, or what the user may read.
 */
export function LanguageSection() {
  const [choice, setChoice] = useState("en");
  const [explicit, setExplicit] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    let mounted = true;
    void api.languagePreference().then((value) => {
      if (!mounted) return;
      setChoice(isLanguage(value.language) ? value.language : "en");
      setExplicit(value.explicit);
    }).catch((reason: unknown) => {
      if (mounted) setError(describeError(reason));
    }).finally(() => {
      if (mounted) setLoading(false);
    });
    return () => { mounted = false; };
  }, []);

  async function save() {
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      const value = await api.setLanguagePreference(choice);
      setChoice(value.language);
      setExplicit(value.explicit);
      // Apply immediately: navigation re-renders in the new language and
      // Urdu script switches the page to right-to-left.
      setLanguage(value.language);
      setSaved(true);
    } catch (reason) {
      setError(describeError(reason));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card aria-labelledby="language-title">
      <CardHeader
        icon={<Languages />}
        title={<span id="language-title">Language</span>}
        description="The language replies and navigation are shown in. Urdu script reads right-to-left."
      />
      <CardBody className="grid gap-4">
        {error && <Alert tone="danger">{error}</Alert>}
        {saved && <Alert tone="success">Language saved.</Alert>}
        {loading ? (
          <p className="flex items-center gap-2 text-xs text-muted"><Spinner size={14} /> Loading your language…</p>
        ) : (
          <>
            <Field label="Your language">
              <div className="grid gap-2" role="radiogroup" aria-label="Your language">
                {LANGUAGES.map((option) => (
                  <label key={option.id} className="flex cursor-pointer items-start gap-2 rounded-lg border border-border px-3 py-2 text-sm hover:bg-surface-2">
                    <input
                      type="radio"
                      name="language"
                      value={option.id}
                      checked={choice === option.id}
                      onChange={() => { setChoice(option.id); setSaved(false); }}
                      className="mt-0.5"
                    />
                    <span>
                      <span className="block font-medium text-foreground">{option.label}</span>
                      <span className="block text-xs text-muted">
                        {option.id === "en" && "Replies and navigation in English."}
                        {option.id === "roman_urdu" && "Jawab aur navigation Roman Urdu mein — jaisay aap likhte hain."}
                        {option.id === "urdu" && "جواب اور نیویگیشن اردو رسم الخط میں — صفحہ دائیں سے بائیں ہو جائے گا۔"}
                      </span>
                    </span>
                  </label>
                ))}
              </div>
            </Field>
            {!explicit && <p className="text-xs text-muted">No language saved yet — replies follow the language of each question until you choose one.</p>}
            <div>
              <Button disabled={saving} onClick={() => void save()}>{saving ? "Saving…" : "Save language"}</Button>
            </div>
          </>
        )}
      </CardBody>
    </Card>
  );
}
