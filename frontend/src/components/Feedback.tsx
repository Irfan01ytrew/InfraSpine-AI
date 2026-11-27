// Toast notifications and a promise-based confirm dialog, shared via context.
import { createContext, type ReactNode, useCallback, useContext, useEffect, useRef, useState } from "react";
import { IconAlert, IconCheck, IconInfo, IconX } from "../icons";

type ToastKind = "success" | "error" | "info";
interface Toast {
  id: number;
  kind: ToastKind;
  title: string;
  body?: string;
}

interface ConfirmOptions {
  title: string;
  body: string;
  confirmLabel: string;
  tone?: "primary" | "danger";
}

interface FeedbackApi {
  toast: (kind: ToastKind, title: string, body?: string) => void;
  confirm: (options: ConfirmOptions) => Promise<boolean>;
}

const FeedbackContext = createContext<FeedbackApi | null>(null);

export function useFeedback(): FeedbackApi {
  const ctx = useContext(FeedbackContext);
  if (!ctx) throw new Error("useFeedback must be used inside <FeedbackProvider>");
  return ctx;
}

export function FeedbackProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [dialog, setDialog] = useState<(ConfirmOptions & { resolve: (ok: boolean) => void }) | null>(null);
  const nextId = useRef(1);

  const dismiss = useCallback((id: number) => setToasts((all) => all.filter((t) => t.id !== id)), []);

  const toast = useCallback(
    (kind: ToastKind, title: string, body?: string) => {
      const id = nextId.current++;
      setToasts((all) => [...all.slice(-3), { id, kind, title, body }]);
      window.setTimeout(() => dismiss(id), kind === "error" ? 8000 : 5000);
    },
    [dismiss],
  );

  const confirm = useCallback(
    (options: ConfirmOptions) => new Promise<boolean>((resolve) => setDialog({ ...options, resolve })),
    [],
  );

  function close(ok: boolean) {
    dialog?.resolve(ok);
    setDialog(null);
  }

  return (
    <FeedbackContext.Provider value={{ toast, confirm }}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} className={`toast toast-${t.kind}`}>
            <span className="toast-icon">
              {t.kind === "success" ? <IconCheck size={16} /> : t.kind === "error" ? <IconAlert size={16} /> : <IconInfo size={16} />}
            </span>
            <div className="toast-text">
              <strong>{t.title}</strong>
              {t.body && <p>{t.body}</p>}
            </div>
            <button className="icon-btn" onClick={() => dismiss(t.id)} aria-label="Dismiss">
              <IconX size={14} />
            </button>
          </div>
        ))}
      </div>
      {dialog && <ConfirmDialog {...dialog} onClose={close} />}
    </FeedbackContext.Provider>
  );
}

function ConfirmDialog({ title, body, confirmLabel, tone = "primary", onClose }: ConfirmOptions & { onClose: (ok: boolean) => void }) {
  const confirmButton = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    confirmButton.current?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="modal-backdrop" onMouseDown={() => onClose(false)}>
      <div className="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title" onMouseDown={(e) => e.stopPropagation()}>
        <h3 id="modal-title">{title}</h3>
        <p className="muted">{body}</p>
        <div className="modal-actions">
          <button className="btn" onClick={() => onClose(false)}>
            Cancel
          </button>
          <button ref={confirmButton} className={`btn ${tone === "danger" ? "btn-danger" : "btn-primary"}`} onClick={() => onClose(true)}>
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
