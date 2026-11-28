import { useState } from "react";
import { DocumentParsersSection } from "./DocumentParsersSection";
import { EmbeddingModelsPage } from "./EmbeddingModelsPage";
import { FolderImportProgressSection } from "./FolderImportProgressSection";
import { FolderSyncSection } from "./FolderSyncSection";
import { MediaTranscriptionSection } from "./MediaTranscriptionSection";
import { OcrEngineSection } from "./OcrEngineSection";
import { OrgSettingsSection } from "./OrgSettingsSection";
import { RoutingPolicySection } from "./RoutingPolicySection";
import { VectorStoreSection } from "./VectorStoreSection";
import { BankConnectorSection } from "../finance/BankConnectorSection";
import { LanguageSection } from "./LanguageSection";
import { AutopilotSection } from "../finance/AutopilotSection";
import { ScheduledReportsSection } from "../reports/ScheduledReportsSection";
import { PageHeader, Tabs } from "./ui";

type SettingsTab = "organization" | "models" | "documents" | "media" | "index" | "imports" | "finance" | "language" | "reports";

const TABS: { value: SettingsTab; label: string; description: string }[] = [
  { value: "organization", label: "Organization", description: "The organization's time zone decides what \"today\" means for delegations, budgets and due dates." },
  { value: "models", label: "AI models", description: "The local model that turns documents into searchable meaning." },
  { value: "documents", label: "Documents & OCR", description: "Readers for PDF, Office and images, and text recognition for scans." },
  { value: "media", label: "Audio & video", description: "Local speech-to-text and on-screen text from video frames." },
  { value: "index", label: "Search index", description: "Where document vectors are stored, and its health." },
  { value: "imports", label: "Imports & sync", description: "Large folder imports and scheduled folder synchronization." },
  { value: "finance", label: "Finance", description: "Payment connector for invoices: which bank connector is active and where the choice comes from. The finance autopilot picks up waiting batches on the worker schedule." },
  { value: "language", label: "Language", description: "The language replies and navigation are shown in: English, Roman Urdu, or Urdu script." },
  { value: "reports", label: "Reports", description: "Scheduled cash summaries, weekly invoices and overdue alerts: who gets them, on which channel, and delivery history." },
];

const STORAGE_KEY = "infraspine_settings_tab";

/** Administrator settings, grouped so each area can be checked on its own. */
export function SettingsPage() {
  const [tab, setTab] = useState<SettingsTab>(() => {
    try { return (localStorage.getItem(STORAGE_KEY) as SettingsTab) || "models"; } catch { return "models"; }
  });
  function choose(next: string) {
    setTab(next as SettingsTab);
    try { localStorage.setItem(STORAGE_KEY, next); } catch { /* storage unavailable */ }
  }
  const current = TABS.find((item) => item.value === tab) ?? TABS[0];

  return (
    <>
      <PageHeader title="Settings" description="How this installation reads, understands and stores your documents." />
      <Tabs className="mb-2" value={current.value} onValueChange={choose} items={TABS.map(({ value, label }) => ({ value, label }))} />
      <p className="mb-5 text-sm text-muted">{current.description}</p>
      <div className="grid gap-5">
        {current.value === "organization" && <OrgSettingsSection />}
        {current.value === "models" && <><EmbeddingModelsPage /><RoutingPolicySection /></>}
        {current.value === "documents" && <><DocumentParsersSection /><OcrEngineSection /></>}
        {current.value === "media" && <MediaTranscriptionSection />}
        {current.value === "index" && <VectorStoreSection />}
        {current.value === "imports" && <><FolderImportProgressSection /><FolderSyncSection /></>}
        {current.value === "finance" && <><BankConnectorSection /><AutopilotSection /></>}
        {current.value === "language" && <LanguageSection />}
        {current.value === "reports" && <ScheduledReportsSection />}
      </div>
    </>
  );
}
