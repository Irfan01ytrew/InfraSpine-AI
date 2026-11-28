// Shared language helpers for the frontend (Step Z slice 4).
//
// Pure functions, unit tested with node --test. The translation table
// covers the core navigation labels (sidebar groups and items) in
// English, Roman Urdu and Urdu script. A language preference never
// changes authorization, permissions, approvals, or what a user may
// read — it only changes how labels are shown and which text direction
// the page uses (Urdu script reads right-to-left).
//
// The document store at the bottom is the single place the current
// language lives in the browser: LanguageSection writes it after a
// successful save, App.tsx loads it once at sign-in, and AdminConsole
// subscribes to it so the sidebar re-renders in the new language.

export const DEFAULT_LANGUAGE = "en";

export const LANGUAGES = [
  { id: "en", label: "English", nativeLabel: "English" },
  { id: "roman_urdu", label: "Roman Urdu", nativeLabel: "Roman Urdu" },
  { id: "urdu", label: "Urdu (script)", nativeLabel: "اردو" },
];

export function isLanguage(value) {
  return LANGUAGES.some((option) => option.id === value);
}

export function normalizeLanguage(value) {
  return isLanguage(value) ? value : DEFAULT_LANGUAGE;
}

/**
 * Core navigation labels, keyed by their English text. Every key must
 * have all three languages, non-empty (pinned by frontend/test/i18n.test.mjs).
 * Numbered module labels keep their "01 · " prefix in every language.
 */
export const NAV_STRINGS = {
  // Group labels
  "Workspace": { en: "Workspace", roman_urdu: "Workspace", urdu: "ورک اسپیس" },
  "InfraSpine modules": { en: "InfraSpine modules", roman_urdu: "InfraSpine modules", urdu: "انفرا اسپائن ماڈیولز" },
  "Research & insights": { en: "Research & insights", roman_urdu: "Research aur insights", urdu: "تحقیق اور بصیرت" },
  "Knowledge": { en: "Knowledge", roman_urdu: "Knowledge", urdu: "علم" },
  "Operations": { en: "Operations", roman_urdu: "Operations", urdu: "آپریشنز" },
  "Administration": { en: "Administration", roman_urdu: "Administration", urdu: "انتظامیہ" },
  // Workspace items
  "Module control": { en: "Module control", roman_urdu: "Module control", urdu: "ماڈیول کنٹرول" },
  "Home": { en: "Home", roman_urdu: "Home", urdu: "ہوم" },
  "AI Agents": { en: "AI Agents", roman_urdu: "AI Agents", urdu: "اے آئی ایجنٹس" },
  "My work": { en: "My work", roman_urdu: "Mera kaam", urdu: "میرا کام" },
  "Meeting minutes": { en: "Meeting minutes", roman_urdu: "Meeting minutes", urdu: "میٹنگ کی کارروائی" },
  "Memory": { en: "Memory", roman_urdu: "Memory", urdu: "میموری" },
  // Numbered modules
  "01 · Knowledge & AI Brain": { en: "01 · Knowledge & AI Brain", roman_urdu: "01 · Knowledge aur AI Brain", urdu: "01 · علم اور اے آئی برین" },
  "02 · IRFANIX AI Agent": { en: "02 · IRFANIX AI Agent", roman_urdu: "02 · IRFANIX AI Agent", urdu: "02 · عرفانکس اے آئی ایجنٹ" },
  "03 · Company intelligence": { en: "03 · Company intelligence", roman_urdu: "03 · Company intelligence", urdu: "03 · کمپنی انٹیلیجنس" },
  "04 · Process intelligence": { en: "04 · Process intelligence", roman_urdu: "04 · Process intelligence", urdu: "04 · عمل کی انٹیلیجنس" },
  "05 · Workflow engine": { en: "05 · Workflow engine", roman_urdu: "05 · Workflow engine", urdu: "05 · ورک فلو انجن" },
  "06 · Governance & security": { en: "06 · Governance & security", roman_urdu: "06 · Governance aur security", urdu: "06 · گورننس اور سیکیورٹی" },
  "07 · Exception & recovery": { en: "07 · Exception & recovery", roman_urdu: "07 · Exception aur recovery", urdu: "07 · استثنیٰ اور بحالی" },
  "08 · Business continuity": { en: "08 · Business continuity", roman_urdu: "08 · Business continuity", urdu: "08 · کاروباری تسلسل" },
  "09 · Frontend & UX": { en: "09 · Frontend & UX", roman_urdu: "09 · Frontend aur UX", urdu: "09 · فرنٹ اینڈ اور یو ایکس" },
  "10 · Integrations": { en: "10 · Integrations", roman_urdu: "10 · Integrations", urdu: "10 · انٹیگریشنز" },
  "11 · Infrastructure": { en: "11 · Infrastructure", roman_urdu: "11 · Infrastructure", urdu: "11 · انفراسٹرکچر" },
  "12 · QA & benchmarking": { en: "12 · QA & benchmarking", roman_urdu: "12 · QA aur benchmarking", urdu: "12 · کیو اے اور بینچ مارکنگ" },
  // Research items
  "Company data research": { en: "Company data research", roman_urdu: "Company data research", urdu: "کمپنی ڈیٹا تحقیق" },
  "Tender & RFP coverage": { en: "Tender & RFP coverage", roman_urdu: "Tender aur RFP coverage", urdu: "ٹینڈر اور آر ایف پی کوریج" },
  "What-if forecasts": { en: "What-if forecasts", roman_urdu: "What-if forecasts", urdu: "کیا ہو اگر پیش گوئیاں" },
  "Market research": { en: "Market research", roman_urdu: "Market research", urdu: "مارکیٹ تحقیق" },
  // Knowledge items
  "Org knowledge": { en: "Org knowledge", roman_urdu: "Org knowledge", urdu: "تنظیمی علم" },
  "Knowledge interviews": { en: "Knowledge interviews", roman_urdu: "Knowledge interviews", urdu: "علمی انٹرویوز" },
  "Knowledge handoffs": { en: "Knowledge handoffs", roman_urdu: "Knowledge handoffs", urdu: "علمی حوالگی" },
  "Expertise directory": { en: "Expertise directory", roman_urdu: "Expertise directory", urdu: "مہارت کی ڈائرکٹری" },
  "Employee training": { en: "Employee training", roman_urdu: "Employee training", urdu: "ملازمین کی تربیت" },
  // Operations items
  "Approvals": { en: "Approvals", roman_urdu: "Approvals", urdu: "منظوریاں" },
  "Process intelligence": { en: "Process intelligence", roman_urdu: "Process intelligence", urdu: "عمل کی انٹیلیجنس" },
  "Invoices & payments": { en: "Invoices & payments", roman_urdu: "Invoices aur payments", urdu: "انوائسز اور ادائیگیاں" },
  "Claims": { en: "Claims", roman_urdu: "Claims", urdu: "دعوے" },
  "Workflows & tasks": { en: "Workflows & tasks", roman_urdu: "Workflows aur tasks", urdu: "ورک فلوز اور کام" },
  "Agent profiles": { en: "Agent profiles", roman_urdu: "Agent profiles", urdu: "ایجنٹ پروفائلز" },
  "Help & requests": { en: "Help & requests", roman_urdu: "Help aur requests", urdu: "مدد اور درخواستیں" },
  "Request forms": { en: "Request forms", roman_urdu: "Request forms", urdu: "درخواست فارم" },
  "Budgets & spend": { en: "Budgets & spend", roman_urdu: "Budgets aur spend", urdu: "بجٹ اور اخراجات" },
  "My departments": { en: "My departments", roman_urdu: "Mere departments", urdu: "میرے محکمے" },
  "Usage reports": { en: "Usage reports", roman_urdu: "Usage reports", urdu: "استعمال کی رپورٹس" },
  "Invite employees": { en: "Invite employees", roman_urdu: "Invite employees", urdu: "ملازمین کو مدعو کریں" },
  // Administration items
  "People & access": { en: "People & access", roman_urdu: "People aur access", urdu: "لوگ اور رسائی" },
  "Partner access": { en: "Partner access", roman_urdu: "Partner access", urdu: "پارٹنر رسائی" },
  "Client workspaces": { en: "Client workspaces", roman_urdu: "Client workspaces", urdu: "کلائنٹ ورک اسپیسز" },
  "Cloud connectors": { en: "Cloud connectors", roman_urdu: "Cloud connectors", urdu: "کلاؤڈ کنیکٹرز" },
  "Webhooks": { en: "Webhooks", roman_urdu: "Webhooks", urdu: "ویب ہکس" },
  "AI providers": { en: "AI providers", roman_urdu: "AI providers", urdu: "اے آئی فراہم کنندگان" },
  "Settings": { en: "Settings", roman_urdu: "Settings", urdu: "ترتیبات" },
  "Company setup": { en: "Company setup", roman_urdu: "Company setup", urdu: "کمپنی سیٹ اپ" },
  "Knowledge Quality": { en: "Knowledge Quality", roman_urdu: "Knowledge Quality", urdu: "علمی معیار" },
  "Audit logs": { en: "Audit logs", roman_urdu: "Audit logs", urdu: "آڈٹ لاگز" },
};

/** Translate one navigation label; unknown labels and unknown languages fall back to the English text. */
export function tNav(label, language) {
  const entry = NAV_STRINGS[label];
  if (!entry) return label;
  return entry[normalizeLanguage(language)] ?? entry.en;
}

/** Only Urdu script reads right-to-left; Roman Urdu stays left-to-right. */
export function isRtlLanguage(language) {
  return normalizeLanguage(language) === "urdu";
}

/**
 * BCP 47 locale for Intl formatting: Roman Urdu uses the Urdu locale
 * with Latin digits (ur-PK-u-nu-latn); Urdu script uses Urdu digits.
 */
export function localeForLanguage(language) {
  const lang = normalizeLanguage(language);
  if (lang === "urdu") return "ur-PK";
  if (lang === "roman_urdu") return "ur-PK-u-nu-latn";
  return "en-US";
}

/**
 * Translate a whole navigation tree (groups + items) without touching
 * anything else — icons, keywords, views and order are preserved.
 */
export function translateNavGroups(groups, language) {
  return (groups ?? []).map((group) => ({
    ...group,
    label: tNav(group.label, language),
    items: (group.items ?? []).map((item) => ({ ...item, label: tNav(item.label, language) })),
  }));
}

// ---------------------------------------------------------------------------
// Browser store: the current language, shared by App, Console and Settings.
// ---------------------------------------------------------------------------

let currentLanguage = DEFAULT_LANGUAGE;
const listeners = new Set();

export function getLanguage() {
  return currentLanguage;
}

export function subscribeLanguage(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function documentLangTag(lang) {
  return lang === "roman_urdu" ? "ur-Latn" : lang === "urdu" ? "ur" : "en";
}

/**
 * Apply a language to the page: html lang + text direction. No-ops
 * without a document (node tests). The manual direction toggle in the
 * user chip stores "infraspine_direction"; a language change clears
 * that override so the preference wins from then on.
 */
export function applyLanguageToDocument(language) {
  const lang = normalizeLanguage(language);
  if (typeof document === "undefined") return;
  document.documentElement.lang = documentLangTag(lang);
  document.documentElement.dir = isRtlLanguage(lang) ? "rtl" : "ltr";
  try {
    localStorage.removeItem("infraspine_direction");
  } catch {
    // storage unavailable — the direction still applies to this page
  }
}

export function setLanguage(language) {
  const next = normalizeLanguage(language);
  if (next === currentLanguage) {
    applyLanguageToDocument(next);
    return;
  }
  currentLanguage = next;
  applyLanguageToDocument(next);
  listeners.forEach((listener) => listener());
}

/**
 * Load a stored preference at sign-in: same as setLanguage, except a
 * manual direction override from the user chip is respected — the page
 * keeps the hand-chosen direction while labels follow the language.
 * The html lang attribute is still set.
 */
export function restoreLanguage(language) {
  const next = normalizeLanguage(language);
  currentLanguage = next;
  if (typeof document !== "undefined") {
    document.documentElement.lang = documentLangTag(next);
    let manual = null;
    try {
      manual = localStorage.getItem("infraspine_direction");
    } catch {
      manual = null;
    }
    if (!manual) document.documentElement.dir = isRtlLanguage(next) ? "rtl" : "ltr";
  }
  listeners.forEach((listener) => listener());
}
