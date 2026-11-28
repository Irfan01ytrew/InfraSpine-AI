import { useEffect, useState, type FormEvent } from "react";
import { api, describeError } from "../api";
import { formatDay } from "../format";
import type { DeploymentOverview, IndustryTemplate, SignedLicense } from "../types";
import { Building2, CheckCircle2, Globe2, KeyRound, LayoutTemplate, Save, Search, ShieldCheck, Wand2 } from "lucide-react";
import { cn } from "../lib/utils";
import { Alert, Badge, Button, Card, CardBody, CardHeader, EmptyState, Field, Input, PageHeader, Select, Textarea } from "./ui";

/** IANA time zones and ISO currencies the browser knows; the server validates the saved value again. */
const TIME_ZONES: string[] = ["UTC", ...supported("timeZone").filter((zone) => zone !== "UTC")];
const CURRENCIES: string[] = supported("currency");
const LOCALES = [
  { value: "", label: "Browser default" },
  { value: "en-US", label: "English (United States)" },
  { value: "en-GB", label: "English (United Kingdom)" },
  { value: "en-PK", label: "English (Pakistan)" },
  { value: "en-IN", label: "English (India)" },
  { value: "en-AE", label: "English (UAE)" },
  { value: "ur-PK", label: "Urdu (Pakistan)" },
  { value: "ar-SA", label: "Arabic (Saudi Arabia)" },
  { value: "ar-AE", label: "Arabic (UAE)" },
  { value: "fr-FR", label: "French (France)" },
  { value: "de-DE", label: "German (Germany)" },
  { value: "es-ES", label: "Spanish (Spain)" },
  { value: "tr-TR", label: "Turkish (Turkey)" },
  { value: "zh-CN", label: "Chinese (China)" },
];

function supported(key: "timeZone" | "currency"): string[] {
  try { return Intl.supportedValuesOf(key); } catch { return []; }
}

function previewFormat(render: () => string): string {
  try { return render(); } catch { return ""; }
}

export function CompanySetupPage({ onProfileSaved }: { onProfileSaved?: () => void | Promise<void> } = {}) {
  const [overview, setOverview] = useState<DeploymentOverview | null>(null);
  const [templates, setTemplates] = useState<IndustryTemplate[]>([]);
  const [templateId, setTemplateId] = useState("");
  const [companyName, setCompanyName] = useState("");
  const [industry, setIndustry] = useState("General");
  const [brandName, setBrandName] = useState("InfraSpine AI");
  const [primaryColor, setPrimaryColor] = useState("#126b4e");
  const [currency, setCurrency] = useState("");
  const [timeZone, setTimeZone] = useState("");
  const [locale, setLocale] = useState("");
  const [licenseText, setLicenseText] = useState("");
  const [templateQuery, setTemplateQuery] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  async function load() {
    try {
      const [result, catalog] = await Promise.all([api.deployment(), api.industryTemplates()]);
      setOverview(result);
      setTemplates(catalog);
      setTemplateId(result.template_id ?? "");
      setCompanyName(result.company_name);
      setIndustry(result.industry);
      setBrandName(result.brand_name);
      setPrimaryColor(result.primary_color);
      setCurrency(result.currency ?? "");
      setTimeZone(result.time_zone ?? "");
      setLocale(result.locale ?? "");
      setError(null);
    } catch (err) { setError(describeError(err)); }
  }
  useEffect(() => { void load(); }, []);

  const selectedTemplate = templates.find((template) => template.template_id === templateId) ?? null;
  const timeZoneInvalid = timeZone !== "" && TIME_ZONES.length > 1 && !TIME_ZONES.includes(timeZone);
  const currencyInvalid = currency !== "" && !/^[A-Z]{3}$/.test(currency);
  const previewDate = previewFormat(() => new Date().toLocaleString(locale || undefined, { dateStyle: "full", timeStyle: "short", timeZone: timeZoneInvalid ? undefined : timeZone || undefined }));
  const previewMoney = currency && !currencyInvalid ? previewFormat(() => new Intl.NumberFormat(locale || undefined, { style: "currency", currency }).format(1234567.89)) : "";

  async function saveCompany(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError(null); setMessage(null);
    try {
      await api.updateCompanyProfile({ company_name: companyName.trim(), industry: industry.trim(), brand_name: brandName.trim(), primary_color: primaryColor, setup_complete: true, template_id: selectedTemplate?.template_id ?? null, template_version: selectedTemplate?.version ?? null, currency: currency || null, time_zone: timeZone || null, locale: locale || null });
      await load();
      await onProfileSaved?.();
      setMessage("Company profile saved on this deployment.");
    } catch (err) { setError(describeError(err)); }
    finally { setBusy(false); }
  }

  async function applyDepartments() {
    if (!selectedTemplate) return;
    setBusy(true); setError(null); setMessage(null);
    try {
      const result = await api.applyIndustryTemplate(selectedTemplate.template_id);
      setMessage(`${selectedTemplate.name} applied: ${result.created_departments.length} departments and ${result.created_roles.length} custom roles created (${result.existing_departments.length + result.existing_roles.length} already existed). Nobody was assigned; invite people in People & access.`);
    } catch (err) { setError(describeError(err)); }
    finally { setBusy(false); }
  }

  async function installLicense(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError(null); setMessage(null);
    try {
      const license = JSON.parse(licenseText) as SignedLicense;
      const status = await api.installLicense(license);
      setOverview((current) => current ? { ...current, license: status } : current);
      setLicenseText(""); setMessage("Signed license verified and installed.");
    } catch (err) { setError(err instanceof SyntaxError ? "The license must be valid JSON." : describeError(err)); }
    finally { setBusy(false); }
  }

  const license = overview?.license;
  const needle = templateQuery.trim().toLowerCase();
  const shownTemplates = templates.filter((item) => !needle || `${item.name} ${item.industry} ${item.description}`.toLowerCase().includes(needle));

  return <>
    <PageHeader
      title="Company setup"
      description="Your organization's identity, industry starter kit and license. Everything stays inside this installation."
      actions={overview && <Badge tone={overview.setup_complete ? "success" : "warning"} dot>{overview.setup_complete ? "Configured" : "Setup required"}</Badge>}
    />
    {error && <Alert tone="danger" className="mb-4">{error}</Alert>}
    {message && <Alert tone="success" className="mb-4">{message}</Alert>}

    <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_340px]">
      <Card>
        <CardHeader icon={<Building2 />} title="Company profile" description="Shown to everyone who signs in." />
        <CardBody>
          <form className="grid gap-4" onSubmit={saveCompany}>
            <div className="grid gap-4 md:grid-cols-2">
              <Field label="Company name"><Input required minLength={2} maxLength={120} value={companyName} onChange={(e) => setCompanyName(e.target.value)} placeholder="e.g. Grand City Developers" /></Field>
              <Field label="Industry" hint="Filled by the starter kit you pick; you can type your own."><Input required minLength={2} maxLength={80} value={industry} onChange={(e) => setIndustry(e.target.value)} /></Field>
              <Field label="Product name shown to employees"><Input required minLength={2} maxLength={48} value={brandName} onChange={(e) => setBrandName(e.target.value)} /></Field>
              <Field label="Brand color">
                <div className="flex items-center gap-2">
                  <input type="color" aria-label="Brand color" value={primaryColor} onChange={(e) => setPrimaryColor(e.target.value)} className="h-9 w-12 cursor-pointer rounded-md border border-border bg-surface p-1" />
                  <Input value={primaryColor} onChange={(e) => /^#[0-9a-fA-F]{0,6}$/.test(e.target.value) && setPrimaryColor(e.target.value)} className="font-mono" aria-label="Brand color hex" />
                </div>
              </Field>
            </div>
            <div className="grid gap-4 border-t border-border pt-4">
              <div>
                <p className="flex items-center gap-2 text-[13px] font-semibold text-foreground"><Globe2 className="size-4 text-muted" /> Regional settings</p>
                <p className="text-xs text-muted">How dates, times and money appear for everyone. Leave a field empty to follow each person's own browser.</p>
              </div>
              <div className="grid gap-4 md:grid-cols-3">
                <Field label="Time zone" error={timeZoneInvalid ? "Pick a time zone from the list, e.g. Asia/Karachi." : undefined}>
                  <Input list="company-time-zones" value={timeZone} onChange={(e) => setTimeZone(e.target.value.trim())} placeholder="Browser default" aria-label="Time zone" />
                  <datalist id="company-time-zones">{TIME_ZONES.map((zone) => <option key={zone} value={zone} />)}</datalist>
                </Field>
                <Field label="Currency" error={currencyInvalid ? "Use a 3-letter currency code, e.g. PKR or USD." : undefined}>
                  <Input list="company-currencies" value={currency} onChange={(e) => setCurrency(e.target.value.trim().toUpperCase().slice(0, 3))} placeholder="Not set" aria-label="Currency" />
                  <datalist id="company-currencies">{CURRENCIES.map((code) => <option key={code} value={code} />)}</datalist>
                </Field>
                <Field label="Number & date format">
                  <Select aria-label="Number and date format" value={locale} onValueChange={setLocale} options={LOCALES} />
                </Field>
              </div>
              <p className="rounded-md bg-surface-2/70 px-3 py-2 text-xs text-foreground-2" aria-live="polite">
                Preview: <strong className="font-medium text-foreground">{previewDate}</strong>{previewMoney && <> · <strong className="font-medium text-foreground">{previewMoney}</strong></>}
              </p>
            </div>
            <div><Button type="submit" variant="primary" loading={busy} disabled={busy || timeZoneInvalid || currencyInvalid}><Save /> Save company profile</Button></div>
          </form>
        </CardBody>
      </Card>

      <Card className="overflow-hidden">
        <CardHeader title="Preview" description="How the sidebar brand will look." />
        <CardBody>
          <div className="flex items-center gap-3 rounded-lg border border-border p-3">
            <span className="grid size-9 place-items-center rounded-lg text-sm font-bold text-white" style={{ background: primaryColor }}>{(brandName.trim()[0] ?? "I").toUpperCase()}</span>
            <span className="truncate text-sm font-semibold text-foreground">{brandName || "Your product"}</span>
          </div>
          <div className="mt-3 h-9 rounded-md text-center text-[13px] leading-9 font-medium text-white" style={{ background: primaryColor }}>Primary button</div>
          <p className="mt-3 text-xs text-muted">{companyName || "Company name"} · {industry || "Industry"}</p>
        </CardBody>
      </Card>
    </div>

    <Card className="mt-5">
      <CardHeader
        icon={<LayoutTemplate />}
        title="Industry starter kits"
        description={`${templates.length} ready-made setups. Applying one creates its departments and custom roles (empty and unassigned); nothing is removed and you can change everything later.`}
        actions={<Input type="search" icon={<Search />} value={templateQuery} onChange={(e) => setTemplateQuery(e.target.value)} placeholder="Search industries" aria-label="Search industries" className="h-8 w-52 text-xs" />}
      />
      <CardBody>
        {shownTemplates.length === 0 ? <EmptyState icon={<Search />} title="No industry matches" description="Pick General organization or type your own industry above." /> : (
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {shownTemplates.map((template) => {
              const selected = template.template_id === templateId;
              return (
                <button
                  key={template.template_id}
                  type="button"
                  aria-pressed={selected}
                  onClick={() => { setTemplateId(selected ? "" : template.template_id); if (!selected) setIndustry(template.industry); }}
                  className={cn("flex cursor-pointer flex-col gap-2 rounded-xl border p-4 text-left outline-none transition-all focus-visible:ring-[3px] focus-visible:ring-ring",
                    selected ? "border-primary bg-primary-soft/60 ring-[3px] ring-ring" : "border-border bg-surface hover:-translate-y-px hover:border-primary/40 hover:shadow-sm")}
                >
                  <span className="flex items-start justify-between gap-2">
                    <span className="text-[14px] font-semibold text-foreground">{template.name}</span>
                    {selected ? <CheckCircle2 className="size-5 shrink-0 text-primary" /> : <span className="text-xs text-muted">v{template.version}</span>}
                  </span>
                  <span className="text-xs leading-relaxed text-muted">{template.description}</span>
                  <span className="flex flex-wrap gap-1">{template.default_departments.map((name) => <Badge key={name}>{name}</Badge>)}</span>
                  {template.default_roles.length > 0 && <span className="flex flex-wrap gap-1">{template.default_roles.map((name) => <Badge key={name} tone="primary">{name}</Badge>)}</span>}
                </button>
              );
            })}
          </div>
        )}
        {selectedTemplate && (
          <div className="mt-4 flex flex-col gap-3 rounded-lg border border-primary/30 bg-primary-soft/40 p-4 md:flex-row md:items-center">
            <div className="min-w-0 flex-1">
              <p className="text-[13px] font-semibold text-foreground">{selectedTemplate.name} selected</p>
              <p className="text-xs text-muted">Creates {selectedTemplate.default_departments.length} departments and {selectedTemplate.default_roles.length} custom roles if missing. Save the profile to record the starter kit.</p>
            </div>
            <Button variant="primary" loading={busy} disabled={busy} onClick={() => void applyDepartments()}><Wand2 /> Apply starter kit</Button>
          </div>
        )}
      </CardBody>
    </Card>

    <Card className="mt-5">
      <CardHeader icon={<KeyRound />} title="License" description="Offline licenses are verified with the publisher's public key; signing keys never belong on this server." actions={<LicenseBadge status={license?.status ?? "missing"} />} />
      <CardBody className="grid gap-5">
        {overview && (
          <dl className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {[
              ["Deployment ID", <code key="d" className="text-xs break-all">{overview.license.deployment_id}</code>],
              ["Enforcement", overview.license.enforcement_enabled ? "Enabled" : "Off (operator setting)"],
              ...(license?.customer ? [["Licensed customer", license.customer]] : []),
              ...(license?.license_id ? [["License ID", license.license_id]] : []),
              ...(license?.expires_at ? [["Expires", formatDay(license.expires_at)]] : []),
              ...(license?.seat_limit ? [["Seats", license.seat_limit.toLocaleString()]] : []),
              ...(license?.features.length ? [["Features", license.features.join(", ")]] : []),
            ].map(([label, value]) => (
              <div key={String(label)} className="rounded-lg border border-border p-3"><dt className="text-xs font-semibold tracking-wide text-muted uppercase">{label}</dt><dd className="mt-1 text-[13px] text-foreground">{value}</dd></div>
            ))}
          </dl>
        )}
        <form className="grid gap-3" onSubmit={installLicense}>
          <Field label="Signed license (JSON from your software publisher)"><Textarea rows={6} spellCheck={false} autoComplete="off" value={licenseText} onChange={(e) => setLicenseText(e.target.value)} placeholder="Paste the signed license envelope" className="font-mono text-xs" /></Field>
          <div><Button type="submit" variant="primary" loading={busy} disabled={busy || !licenseText.trim()}><ShieldCheck /> Verify and install</Button></div>
        </form>
      </CardBody>
    </Card>
  </>;
}

function LicenseBadge({ status }: { status: string }) {
  return <Badge tone={status === "valid" ? "success" : status === "expired" || status === "invalid" ? "danger" : "warning"} dot>{status.replaceAll("_", " ")}</Badge>;
}
