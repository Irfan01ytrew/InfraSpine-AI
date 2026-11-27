import { forwardRef, type InputHTMLAttributes, type ReactNode, type SelectHTMLAttributes, type TextareaHTMLAttributes } from "react";
import { ChevronDown } from "lucide-react";
import { cn } from "../../lib/utils";

const fieldBase =
  "w-full min-w-0 rounded-md border border-border bg-surface text-[13px] text-foreground shadow-xs outline-none " +
  "transition-[border-color,box-shadow] placeholder:text-muted/80 hover:border-border-strong " +
  "focus-visible:border-primary focus-visible:ring-[3px] focus-visible:ring-ring " +
  "disabled:cursor-not-allowed disabled:bg-surface-2 disabled:opacity-70 aria-invalid:border-danger aria-invalid:ring-danger-soft";

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement> & { icon?: ReactNode }>(
  function Input({ className, icon, type = "text", ...props }, ref) {
    const input = <input ref={ref} type={type} className={cn(fieldBase, "h-9 px-3", icon && "pl-9", className)} {...props} />;
    if (!icon) return input;
    return (
      <div className="relative w-full min-w-0">
        <span className="pointer-events-none absolute top-1/2 left-3 -translate-y-1/2 text-muted [&_svg]:size-4">{icon}</span>
        {input}
      </div>
    );
  },
);

export const Textarea = forwardRef<HTMLTextAreaElement, TextareaHTMLAttributes<HTMLTextAreaElement>>(function Textarea(
  { className, ...props },
  ref,
) {
  return <textarea ref={ref} className={cn(fieldBase, "min-h-20 resize-y px-3 py-2 leading-relaxed", className)} {...props} />;
});

/** A styled native select: keeps native keyboard/mobile behavior and works with any existing value shape. */
export const NativeSelect = forwardRef<HTMLSelectElement, SelectHTMLAttributes<HTMLSelectElement> & { wrapperClassName?: string }>(
  function NativeSelect({ className, wrapperClassName, children, ...props }, ref) {
    return (
      <div className={cn("relative w-full min-w-0", wrapperClassName)}>
        <select ref={ref} className={cn(fieldBase, "h-9 cursor-pointer appearance-none truncate py-0 pr-9 pl-3", className)} {...props}>
          {children}
        </select>
        <ChevronDown className="pointer-events-none absolute top-1/2 right-3 size-4 -translate-y-1/2 text-muted" aria-hidden />
      </div>
    );
  },
);

export function Label({ className, ...props }: React.LabelHTMLAttributes<HTMLLabelElement>) {
  return <label className={cn("text-xs font-medium text-foreground-2", className)} {...props} />;
}

export function Field({ label, hint, error, htmlFor, className, children }: {
  label?: ReactNode; hint?: ReactNode; error?: ReactNode; htmlFor?: string; className?: string; children: ReactNode;
}) {
  return (
    <div className={cn("grid gap-1.5", className)}>
      {label && <Label htmlFor={htmlFor}>{label}</Label>}
      {children}
      {error ? <p className="text-xs text-danger-text">{error}</p> : hint ? <p className="text-xs text-muted">{hint}</p> : null}
    </div>
  );
}
