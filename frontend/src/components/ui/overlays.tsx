import type { ComponentProps, ReactNode } from "react";
import { Dialog as D, DropdownMenu as M, Select as S, Tabs as T, Tooltip as TT, Switch as SW, Checkbox as CB } from "radix-ui";
import { Check, ChevronDown, X } from "lucide-react";
import { cn } from "../../lib/utils";

const popover = "z-50 rounded-lg border border-border bg-surface p-1 text-foreground shadow-lg data-[state=open]:animate-in data-[state=closed]:animate-out";

/* --------------------------------------------------------------- Tooltip */
export function Tooltip({ content, children, side = "top" }: { content: ReactNode; children: ReactNode; side?: "top" | "right" | "bottom" | "left" }) {
  return (
    <TT.Provider delayDuration={250}>
      <TT.Root>
        <TT.Trigger asChild>{children}</TT.Trigger>
        <TT.Portal>
          <TT.Content side={side} sideOffset={6} className="z-50 max-w-xs rounded-md bg-foreground px-2 py-1 text-xs text-surface shadow-lg data-[state=delayed-open]:animate-in">
            {content}
          </TT.Content>
        </TT.Portal>
      </TT.Root>
    </TT.Provider>
  );
}

/* ---------------------------------------------------------------- Select */
export type SelectOption = { value: string; label: ReactNode; disabled?: boolean };
export function Select({ value, onValueChange, options, placeholder, className, disabled, "aria-label": ariaLabel, size = "md" }: {
  value: string; onValueChange: (value: string) => void; options: SelectOption[]; placeholder?: string;
  className?: string; disabled?: boolean; "aria-label"?: string; size?: "sm" | "md";
}) {
  // Radix reserves "" for "no value", so empty option values are mapped through a sentinel.
  const EMPTY = "__empty__";
  return (
    <S.Root value={value === "" ? EMPTY : value} onValueChange={(v) => onValueChange(v === EMPTY ? "" : v)} disabled={disabled}>
      <S.Trigger
        aria-label={ariaLabel}
        className={cn(
          "inline-flex w-full min-w-0 cursor-pointer items-center justify-between gap-2 rounded-md border border-border bg-surface px-3 text-[13px] text-foreground shadow-xs outline-none",
          "transition-[border-color,box-shadow] hover:border-border-strong focus-visible:border-primary focus-visible:ring-[3px] focus-visible:ring-ring",
          "disabled:cursor-not-allowed disabled:opacity-60 data-[placeholder]:text-muted",
          size === "sm" ? "h-8 text-xs" : "h-9",
          className,
        )}
      >
        <span className="truncate"><S.Value placeholder={placeholder} /></span>
        <S.Icon asChild><ChevronDown className="size-4 shrink-0 text-muted" /></S.Icon>
      </S.Trigger>
      <S.Portal>
        <S.Content position="popper" sideOffset={4} className={cn(popover, "max-h-[min(360px,var(--radix-select-content-available-height))] min-w-[var(--radix-select-trigger-width)] overflow-hidden")}>
          <S.Viewport>
            {options.map((o) => (
              <S.Item
                key={o.value || EMPTY}
                value={o.value === "" ? EMPTY : o.value}
                disabled={o.disabled}
                className="relative flex cursor-pointer items-center rounded-md py-1.5 pr-8 pl-2.5 text-[13px] outline-none select-none data-[disabled]:pointer-events-none data-[disabled]:opacity-50 data-[highlighted]:bg-surface-2"
              >
                <S.ItemText>{o.label}</S.ItemText>
                <S.ItemIndicator className="absolute right-2.5 text-primary"><Check className="size-4" /></S.ItemIndicator>
              </S.Item>
            ))}
          </S.Viewport>
        </S.Content>
      </S.Portal>
    </S.Root>
  );
}

/* ---------------------------------------------------------- DropdownMenu */
export const DropdownMenu = M.Root;
export const DropdownMenuTrigger = M.Trigger;
export function DropdownMenuContent({ className, align = "end", ...props }: ComponentProps<typeof M.Content>) {
  return (
    <M.Portal>
      <M.Content align={align} sideOffset={6} className={cn(popover, "min-w-44", className)} {...props} />
    </M.Portal>
  );
}
export function DropdownMenuItem({ className, danger, ...props }: ComponentProps<typeof M.Item> & { danger?: boolean }) {
  return (
    <M.Item
      className={cn(
        "flex cursor-pointer items-center gap-2 rounded-md px-2.5 py-1.5 text-[13px] outline-none select-none data-[disabled]:pointer-events-none data-[disabled]:opacity-50 [&_svg]:size-4 [&_svg]:text-muted",
        danger ? "text-danger-text data-[highlighted]:bg-danger-soft [&_svg]:text-danger-text" : "data-[highlighted]:bg-surface-2",
        className,
      )}
      {...props}
    />
  );
}
export function DropdownMenuLabel({ className, ...props }: ComponentProps<typeof M.Label>) {
  return <M.Label className={cn("px-2.5 py-1.5 text-xs font-semibold tracking-wide text-muted uppercase", className)} {...props} />;
}
export function DropdownMenuSeparator() {
  return <M.Separator className="-mx-1 my-1 h-px bg-border" />;
}

/* ---------------------------------------------------------------- Dialog */
export function Dialog({ open, onOpenChange, title, description, children, footer, className }: {
  open: boolean; onOpenChange: (open: boolean) => void; title: ReactNode; description?: ReactNode;
  children?: ReactNode; footer?: ReactNode; className?: string;
}) {
  return (
    <D.Root open={open} onOpenChange={onOpenChange}>
      <D.Portal>
        <D.Overlay className="fixed inset-0 z-50 bg-black/40 backdrop-blur-[2px] data-[state=open]:animate-in" />
        <D.Content className={cn("fixed top-1/2 left-1/2 z-50 grid max-h-[85dvh] w-[calc(100vw-32px)] max-w-lg -translate-x-1/2 -translate-y-1/2 grid-rows-[auto_minmax(0,1fr)_auto] rounded-xl border border-border bg-surface shadow-lg outline-none", className)}>
          <div className="flex items-start justify-between gap-4 border-b border-border px-5 py-4">
            <div>
              <D.Title className="text-[15px] font-semibold text-foreground">{title}</D.Title>
              {description ? <D.Description className="mt-1 text-xs text-muted">{description}</D.Description> : <D.Description className="sr-only">{title}</D.Description>}
            </div>
            <D.Close className="grid size-7 cursor-pointer place-items-center rounded-md text-muted hover:bg-surface-2 hover:text-foreground" aria-label="Close"><X className="size-4" /></D.Close>
          </div>
          <div className="overflow-y-auto px-5 py-4">{children}</div>
          {footer && <div className="flex justify-end gap-2 border-t border-border px-5 py-3">{footer}</div>}
        </D.Content>
      </D.Portal>
    </D.Root>
  );
}

/* ------------------------------------------------------------------ Tabs */
export function Tabs({ value, onValueChange, items, className }: {
  value: string; onValueChange: (value: string) => void; items: { value: string; label: ReactNode; count?: number }[]; className?: string;
}) {
  return (
    <T.Root value={value} onValueChange={onValueChange}>
      <T.List className={cn("inline-flex max-w-full items-center gap-1 overflow-x-auto rounded-lg bg-surface-2 p-1 scrollbar-thin", className)}>
        {items.map((it) => (
          <T.Trigger
            key={it.value}
            value={it.value}
            className="inline-flex h-7 cursor-pointer items-center gap-1.5 rounded-md px-3 text-xs font-medium whitespace-nowrap text-muted outline-none transition-colors hover:text-foreground focus-visible:ring-[3px] focus-visible:ring-ring data-[state=active]:bg-surface data-[state=active]:text-foreground data-[state=active]:shadow-xs"
          >
            {it.label}
            {it.count !== undefined && <span className="rounded-full bg-border px-1.5 text-xs leading-4 text-foreground-2">{it.count}</span>}
          </T.Trigger>
        ))}
      </T.List>
    </T.Root>
  );
}

/* ------------------------------------------------------- Switch/Checkbox */
export function Switch({ className, ...props }: ComponentProps<typeof SW.Root>) {
  return (
    <SW.Root className={cn("inline-flex h-5 w-9 shrink-0 cursor-pointer items-center rounded-full border border-transparent bg-border-strong outline-none transition-colors focus-visible:ring-[3px] focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50 data-[state=checked]:bg-primary", className)} {...props}>
      <SW.Thumb className="block size-4 translate-x-0.5 rounded-full bg-white shadow-sm transition-transform data-[state=checked]:translate-x-[17px]" />
    </SW.Root>
  );
}
export function Checkbox({ className, ...props }: ComponentProps<typeof CB.Root>) {
  return (
    <CB.Root className={cn("grid size-4 shrink-0 cursor-pointer place-items-center rounded-[4px] border border-border-strong bg-surface outline-none focus-visible:ring-[3px] focus-visible:ring-ring disabled:opacity-50 data-[state=checked]:border-primary data-[state=checked]:bg-primary data-[state=checked]:text-primary-foreground", className)} {...props}>
      <CB.Indicator><Check className="size-3" strokeWidth={3} /></CB.Indicator>
    </CB.Root>
  );
}
