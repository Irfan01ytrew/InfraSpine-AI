// ChatGPT-style conversation list: new chat, search, date groups, inline rename and delete.
import { type KeyboardEvent, useState } from "react";
import { MessageSquare, MoreHorizontal, Pencil, Search, SquarePen, Trash2 } from "lucide-react";
import { cn } from "../lib/utils";
import { Button, DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger, Input, Skeleton } from "./ui";
import type { ConversationSummary } from "../types";

interface Props {
  conversations: ConversationSummary[] | null;
  activeId: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
  onRename: (id: string, title: string) => Promise<void>;
  onDelete: (conversation: ConversationSummary) => void;
}

function groupLabel(updatedAt: string): string {
  const day = 86_400_000;
  const startOfToday = new Date();
  startOfToday.setHours(0, 0, 0, 0);
  const t = new Date(updatedAt).getTime();
  if (t >= startOfToday.getTime()) return "Today";
  if (t >= startOfToday.getTime() - day) return "Yesterday";
  if (t >= startOfToday.getTime() - 7 * day) return "Previous 7 days";
  if (t >= startOfToday.getTime() - 30 * day) return "Previous 30 days";
  return "Older";
}

export function ConversationSidebar({ conversations, activeId, onSelect, onNew, onRename, onDelete }: Props) {
  const [query, setQuery] = useState("");
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");

  const needle = query.trim().toLowerCase();
  const visible = (conversations ?? []).filter((c) => !needle || c.title.toLowerCase().includes(needle));
  const groups: [string, ConversationSummary[]][] = [];
  for (const c of visible) {
    const label = groupLabel(c.updated_at);
    const group = groups.find(([name]) => name === label);
    if (group) group[1].push(c);
    else groups.push([label, [c]]);
  }

  async function commitRename(id: string) {
    const title = draft.trim();
    setEditing(null);
    const current = conversations?.find((c) => c.id === id)?.title;
    if (title && title !== current) await onRename(id, title);
  }

  function onRenameKey(event: KeyboardEvent<HTMLInputElement>, id: string) {
    if (event.key === "Enter") void commitRename(id);
    if (event.key === "Escape") setEditing(null);
  }

  return (
    <aside className="flex min-h-0 flex-col border-border bg-surface-2/50 max-lg:max-h-72 max-lg:border-b lg:border-r rtl:lg:border-r-0 rtl:lg:border-l" aria-label="Conversations">
      <div className="grid shrink-0 gap-2 p-3">
        <Button variant="primary" className="w-full" onClick={onNew}>
          <SquarePen /> New chat
        </Button>
        <Input type="search" icon={<Search />} placeholder="Search chats" value={query} onChange={(e) => setQuery(e.target.value)} aria-label="Search chats" className="h-8 text-xs" />
      </div>
      <nav className="scrollbar-thin min-h-0 flex-1 overflow-y-auto px-2 pb-3">
        {conversations === null ? (
          <div className="grid gap-1.5 p-1">{[0, 1, 2, 3, 4].map((i) => <Skeleton key={i} className="h-8 w-full" />)}</div>
        ) : visible.length === 0 ? (
          <p className="px-3 py-6 text-center text-xs text-muted">{needle ? "No chats match your search." : "Your conversations will appear here."}</p>
        ) : (
          groups.map(([label, items]) => (
            <section key={label} className="mt-2 first:mt-0">
              <h4 className="px-2.5 pt-2 pb-1 text-xs font-semibold tracking-[0.08em] text-muted uppercase">{label}</h4>
              <ul className="grid gap-px">
                {items.map((c) => {
                  const active = c.id === activeId;
                  return (
                    <li key={c.id} className={cn("group relative flex min-w-0 items-center rounded-md", active ? "bg-surface shadow-xs ring-1 ring-border" : "hover:bg-surface")}>
                      {editing === c.id ? (
                        <input
                          className="m-0.5 h-8 w-full rounded-md border border-primary bg-surface px-2 text-[13px] text-foreground ring-[3px] ring-ring outline-none"
                          value={draft}
                          autoFocus
                          maxLength={200}
                          onChange={(e) => setDraft(e.target.value)}
                          onKeyDown={(e) => onRenameKey(e, c.id)}
                          onBlur={() => void commitRename(c.id)}
                          aria-label="Conversation title"
                        />
                      ) : (
                        <>
                          <button
                            type="button"
                            className={cn("flex h-9 min-w-0 flex-1 cursor-pointer items-center gap-2 rounded-md px-2.5 text-left text-[13px] outline-none focus-visible:ring-[3px] focus-visible:ring-ring", active ? "font-medium text-foreground" : "text-foreground-2")}
                            onClick={() => onSelect(c.id)}
                            title={c.title}
                            aria-current={active ? "true" : undefined}
                          >
                            <MessageSquare className={cn("size-3.5 shrink-0", active ? "text-primary" : "text-muted")} />
                            <span className="truncate">{c.title}</span>
                          </button>
                          <DropdownMenu>
                            <DropdownMenuTrigger asChild>
                              <button
                                type="button"
                                aria-label={`Options for ${c.title}`}
                                className={cn("mr-1 grid size-7 shrink-0 cursor-pointer place-items-center rounded-md text-muted outline-none hover:bg-surface-2 hover:text-foreground focus-visible:opacity-100 data-[state=open]:opacity-100", active ? "opacity-100" : "opacity-0 group-hover:opacity-100 max-lg:opacity-100")}
                              >
                                <MoreHorizontal className="size-4" />
                              </button>
                            </DropdownMenuTrigger>
                            <DropdownMenuContent align="start">
                              <DropdownMenuItem onSelect={() => { setDraft(c.title); setEditing(c.id); }}><Pencil /> Rename</DropdownMenuItem>
                              <DropdownMenuItem danger onSelect={() => onDelete(c)}><Trash2 /> Delete</DropdownMenuItem>
                            </DropdownMenuContent>
                          </DropdownMenu>
                        </>
                      )}
                    </li>
                  );
                })}
              </ul>
            </section>
          ))
        )}
      </nav>
    </aside>
  );
}
