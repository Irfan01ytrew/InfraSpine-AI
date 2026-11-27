import { useEffect, useRef, useState } from "react";
import { Check, Copy } from "lucide-react";
import { Button } from "./ui";

export function CopyButton({ text, label = "Copy answer", compact = false }: { text: string; label?: string; compact?: boolean }) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);
  async function copy() {
    clearTimeout(timer.current);
    try {
      await navigator.clipboard.writeText(text);
      setState("copied");
    } catch {
      setState("failed");
    }
    timer.current = setTimeout(() => setState("idle"), 2500);
  }
  return (
    <span className="inline-flex items-center gap-1">
      <Button size={compact ? "icon-xs" : "sm"} variant="ghost" onClick={() => void copy()} aria-label={label} title={label}>
        {state === "copied" ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
        {!compact && (state === "copied" ? "Copied" : "Copy")}
      </Button>
      <span role="status" className={state === "failed" ? "text-xs text-danger-text" : "sr-only"}>
        {state === "failed" ? "Copy unavailable. Select the text to copy." : state === "copied" ? "Copied to clipboard" : ""}
      </span>
    </span>
  );
}
