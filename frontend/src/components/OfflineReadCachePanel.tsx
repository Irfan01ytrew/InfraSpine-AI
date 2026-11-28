// Saved Knowledge answers, shown read-only while offline (Step U, slice 5).
//
// The answers here are the user's own last-known answers, saved on this
// device when they were received. They are stale the moment they are
// stored: each one is labelled with when it was saved, and the panel says
// so plainly. Nothing here can be acted on — there is no ask, approve or
// apply path; the only action is clearing the saved copies. For a fresh
// answer, ask again once back online.
import { useState } from "react";
import { ChevronDown, Trash2 } from "lucide-react";
import { staleLabel } from "../lib/offlineReadCache.mjs";
import type { SavedKnowledgeAnswer } from "../lib/offlineReadCache.mjs";
import { cn } from "../lib/utils";
import { Markdown } from "./Markdown";
import { Button, Card } from "./ui";

export function OfflineReadCachePanel({
  answers,
  onClear,
}: {
  answers: SavedKnowledgeAnswer[];
  onClear: () => void;
}) {
  const [openId, setOpenId] = useState<string | null>(answers[0]?.id ?? null);
  if (answers.length === 0) return null;
  return (
    <Card className="overflow-hidden">
      <div className="border-b border-border px-5 py-3.5">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-[14px] font-semibold text-foreground">
            Saved answers · offline copy
          </h2>
          <Button variant="ghost" size="sm" onClick={onClear}>
            <Trash2 /> Clear saved answers
          </Button>
        </div>
        <p className="mt-1 text-xs leading-relaxed text-muted">
          You are offline. These are the last answers you received on this device — read-only, and
          they may be out of date. Ask again when you are back online for a fresh answer, and never
          use a saved answer for a decision.
        </p>
      </div>
      <ul className="divide-y divide-border">
        {answers.map((saved) => {
          const open = openId === saved.id;
          return (
            <li key={saved.id}>
              <button
                type="button"
                className="flex w-full cursor-pointer items-center gap-2 px-5 py-3 text-left hover:bg-surface-2"
                onClick={() => setOpenId(open ? null : saved.id)}
                aria-expanded={open}
              >
                <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-foreground">
                  {saved.question}
                </span>
                <span className="shrink-0 text-xs text-muted">{staleLabel(saved.savedAt)}</span>
                <ChevronDown className={cn("size-4 shrink-0 text-muted transition-transform", open && "rotate-180")} />
              </button>
              {open && (
                <div className="grid gap-2 px-5 pb-4 text-[13px] leading-relaxed text-foreground">
                  {saved.insufficientEvidence && (
                    <p className="text-xs font-medium text-muted" role="note">
                      When this was answered, the AI said there was not enough evidence for a
                      complete answer.
                    </p>
                  )}
                  <Markdown text={saved.answer} />
                  {saved.citations.length > 0 && (
                    <p className="text-xs text-muted">
                      Sources:{" "}
                      {saved.citations
                        .map((c) => `${c.filename} · page ${c.page_number}`)
                        .join("; ")}
                    </p>
                  )}
                  {(saved.model || saved.provider) && (
                    <p className="text-xs text-muted">
                      Answered by {[saved.provider, saved.model].filter(Boolean).join(" · ")}
                    </p>
                  )}
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </Card>
  );
}
