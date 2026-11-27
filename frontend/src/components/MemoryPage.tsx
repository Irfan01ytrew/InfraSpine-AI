import { useCallback, useEffect, useState } from "react";
import { api, describeError } from "../api";
import { formatDate, timeAgo } from "../format";
import { IconBrain, IconPencil, IconTrash, Spinner } from "../icons";
import type { MemoryItem, MemoryKind, MemoryList } from "../types";
import { useFeedback } from "./Feedback";
import { Alert, Button, PageHeader } from "./ui";
import { useLiveRefresh } from "../lib/useLiveRefresh";

const MAX_CHARS = 500; // matches the server and database limit

const KIND_LABEL: Record<MemoryKind, string> = {
  preference: "Preference",
  terminology: "Terminology",
  fact: "About me",
};

/** Client-side check before sending; the server validates again. */
function validate(text: string): string | null {
  const value = text.trim();
  if (!value) return "Write something to remember.";
  if (value.length > MAX_CHARS) return `Keep it under ${MAX_CHARS} characters (${value.length} now).`;
  return null;
}

/** The signed-in user's personal memory. Only the owner can see these; they are never shared or cited. */
export function MemoryPage() {
  const { toast, confirm } = useFeedback();
  const [list, setList] = useState<MemoryList | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [kind, setKind] = useState<MemoryKind | "auto">("auto");
  const [touched, setTouched] = useState(false);
  const [saving, setSaving] = useState(false);
  const [editing, setEditing] = useState<{ id: string; text: string } | null>(null);

  const load = useCallback(async () => {
    try {
      setList(await api.memories());
      setLoadError(null);
    } catch (err) {
      setLoadError(describeError(err));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);
  useLiveRefresh(load);

  const draftError = validate(draft);
  const full = list !== null && list.active_count >= list.max_items;

  async function add(event: React.FormEvent) {
    event.preventDefault();
    setTouched(true);
    if (draftError || saving) return;
    setSaving(true);
    try {
      const result = await api.createMemory(draft.trim(), kind === "auto" ? undefined : kind);
      const message =
        result.outcome === "duplicate"
          ? "Already remembered"
          : result.outcome === "superseded"
            ? "Updated the earlier memory on this subject"
            : "Remembered";
      toast("success", message);
      setDraft("");
      setTouched(false);
      await load();
    } catch (err) {
      toast("error", "Could not save", describeError(err));
    } finally {
      setSaving(false);
    }
  }

  async function saveEdit(item: MemoryItem) {
    if (!editing) return;
    const error = validate(editing.text);
    if (error) return toast("error", "Could not save", error);
    try {
      await api.updateMemory(item.id, { content: editing.text.trim() });
      setEditing(null);
      toast("success", "Memory updated");
      await load();
    } catch (err) {
      toast("error", "Could not save", describeError(err));
    }
  }

  async function remove(item: MemoryItem) {
    const ok = await confirm({
      title: "Forget this memory?",
      body: "It is deleted together with its earlier versions and is no longer used in answers.",
      confirmLabel: "Forget",
      tone: "danger",
    });
    if (!ok) return;
    try {
      await api.deleteMemory(item.id);
      toast("success", "Forgotten");
      await load();
    } catch (err) {
      toast("error", "Could not delete", describeError(err));
    }
  }

  async function clearAll() {
    const ok = await confirm({
      title: "Forget everything?",
      body: "All of your memories and their history are permanently deleted.",
      confirmLabel: "Forget all",
      tone: "danger",
    });
    if (!ok) return;
    try {
      const { deleted } = await api.clearMemories();
      toast("success", `Forgot ${deleted} ${deleted === 1 ? "memory" : "memories"}`);
      await load();
    } catch (err) {
      toast("error", "Could not delete", describeError(err));
    }
  }

  return (
    <>
      <PageHeader
        title="Memory"
        description={<>Preferences and personal context InfraSpine uses to tailor your answers. Only you can see them; they are never shared with others and never cited as sources. You can also say "remember that …" or "forget …" in chat.</>}
        actions={<>{list && list.items.length > 0 && (
          <Button variant="ghost" onClick={() =>void clearAll()}>
            <IconTrash size={14} /> Forget all</Button>
        )}</>}
      />

      <section className="card memory-add">
        <form onSubmit={(e) => void add(e)} noValidate>
          <label className="field">
            <span className="field-label">Add a memory</span>
            <textarea
              className={`memory-input ${touched && draftError ? "input-invalid" : ""}`}
              rows={2}
              maxLength={MAX_CHARS + 50}
              placeholder='For example: "I prefer short answers with bullet points" or "My team is Finance"'
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onBlur={() => setTouched(true)}
              aria-invalid={touched && !!draftError}
              disabled={full}
            />
            <span className={`memory-hint ${touched && draftError && draft ? "memory-hint-error" : ""}`}>
              {touched && draftError && draft ? draftError : `${draft.trim().length}/${MAX_CHARS}`}
            </span>
          </label>
          <div className="memory-add-actions">
            <div className="segmented" role="radiogroup" aria-label="Kind of memory">
              {(["auto", "preference", "terminology", "fact"] as const).map((k) => (
                <button
                  key={k}
                  type="button"
                  role="radio"
                  aria-checked={kind === k}
                  className={kind === k ? "segment segment-active" : "segment"}
                  onClick={() => setKind(k)}
                >
                  {k === "auto" ? "Auto" : KIND_LABEL[k]}
                </button>
              ))}
            </div>
            <button className="btn btn-primary" type="submit" disabled={saving || full || !!draftError}>
              {saving ? <Spinner size={14} /> : null} Remember
            </button>
          </div>
          {full && <p className="memory-hint memory-hint-error">You have reached the limit of {list?.max_items} memories.</p>}
        </form>
      </section>

      {loadError ? (
        <Alert tone="danger" className="mb-4">{loadError}</Alert>
      ) : !list ? (
        <div className="loading">
          <Spinner size={22} />
        </div>
      ) : list.items.length === 0 ? (
        <section className="card empty memory-empty">
          <IconBrain size={20} /> Nothing remembered yet.
        </section>
      ) : (
        <section className="card card-flush">
          <ul className="memory-list">
            {list.items.map((item) => (
              <li key={item.id} className="memory-item">
                <div className="memory-body">
                  <div className="memory-meta">
                    <span className="chip chip-sm">{KIND_LABEL[item.kind]}</span>
                    {item.subject && <span className="muted">{item.subject}</span>}
                    <span className="muted" title={formatDate(item.created_at)}>
                      · {item.source === "chat" ? "from chat" : "added"} {timeAgo(item.created_at)}
                    </span>
                  </div>
                  {editing?.id === item.id ? (
                    <div className="memory-edit">
                      <textarea
                        className={`memory-input ${validate(editing.text) ? "input-invalid" : ""}`}
                        rows={2}
                        value={editing.text}
                        onChange={(e) => setEditing({ id: item.id, text: e.target.value })}
                        autoFocus
                      />
                      <div className="memory-edit-actions">
                        <span className="memory-hint">{editing.text.trim().length}/{MAX_CHARS}</span>
                        <button className="btn btn-sm" type="button" onClick={() => setEditing(null)}>
                          Cancel
                        </button>
                        <button
                          className="btn btn-sm btn-primary"
                          type="button"
                          disabled={!!validate(editing.text)}
                          onClick={() => void saveEdit(item)}
                        >
                          Save
                        </button>
                      </div>
                    </div>
                  ) : (
                    <p className="memory-content">{item.content}</p>
                  )}
                </div>
                {editing?.id !== item.id && (
                  <div className="memory-actions">
                    <button
                      className="icon-btn"
                      aria-label="Edit memory"
                      title="Edit"
                      onClick={() => setEditing({ id: item.id, text: item.content })}
                    >
                      <IconPencil size={14} />
                    </button>
                    <button className="icon-btn" aria-label="Forget memory" title="Forget" onClick={() => void remove(item)}>
                      <IconTrash size={14} />
                    </button>
                  </div>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}
    </>
  );
}
