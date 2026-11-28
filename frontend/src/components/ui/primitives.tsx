import type { HTMLAttributes, ReactNode } from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { AlertTriangle, CheckCircle2, Info, XCircle } from "lucide-react";
import { cn } from "../../lib/utils";

/* ------------------------------------------------------------------ Card */
export function Card({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("rounded-xl border border-border bg-surface shadow-xs", className)} {...props} />;
}
export function CardHeader({ title, description, actions, icon, className }: {
  title: ReactNode; description?: ReactNode; actions?: ReactNode; icon?: ReactNode; className?: string;
}) {
  return (
    <div className={cn("flex flex-wrap items-start justify-between gap-3 border-b border-border px-5 py-4", className)}>
      <div className="flex min-w-0 items-start gap-3">
        {icon && <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-primary-soft text-primary [&_svg]:size-[18px]">{icon}</span>}
        <div className="min-w-0">
          <h3 className="text-sm font-semibold text-foreground">{title}</h3>
          {description && <p className="mt-0.5 text-xs leading-relaxed text-muted">{description}</p>}
        </div>
      </div>
      {actions && <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}
export function CardBody({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("p-5", className)} {...props} />;
}

/* ----------------------------------------------------------------- Badge */
export const badgeVariants = cva(
  "inline-flex shrink-0 items-center gap-1 whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-medium leading-4 [&_svg]:size-3",
  {
    variants: {
      tone: {
        neutral: "bg-surface-2 text-foreground-2 ring-1 ring-border ring-inset",
        primary: "bg-primary-soft text-primary",
        success: "bg-success-soft text-success-text",
        warning: "bg-warning-soft text-warning-text",
        danger: "bg-danger-soft text-danger-text",
      },
    },
    defaultVariants: { tone: "neutral" },
  },
);
export function Badge({ className, tone, dot, children, ...props }: HTMLAttributes<HTMLSpanElement> & VariantProps<typeof badgeVariants> & { dot?: boolean }) {
  return (
    <span className={cn(badgeVariants({ tone }), className)} {...props}>
      {dot && <span className="size-1.5 rounded-full bg-current" aria-hidden />}
      {children}
    </span>
  );
}

/* ----------------------------------------------------------------- Alert */
const alertIcons = { info: Info, success: CheckCircle2, warning: AlertTriangle, danger: XCircle };
const alertTones = {
  info: "border-primary/25 bg-primary-soft text-foreground [&>svg]:text-primary",
  success: "border-success/30 bg-success-soft text-success-text",
  warning: "border-warning/30 bg-warning-soft text-warning-text",
  danger: "border-danger/30 bg-danger-soft text-danger-text",
};
export function Alert({ tone = "info", title, children, action, className }: {
  tone?: keyof typeof alertTones; title?: ReactNode; children?: ReactNode; action?: ReactNode; className?: string;
}) {
  const Icon = alertIcons[tone];
  return (
    <div role={tone === "danger" ? "alert" : "status"} className={cn("flex items-start gap-3 rounded-lg border px-4 py-3 text-[13px]", alertTones[tone], className)}>
      <Icon className="mt-0.5 size-4 shrink-0" aria-hidden />
      <div className="min-w-0 flex-1 leading-relaxed">
        {title && <p className="font-semibold">{title}</p>}
        {children && <div className={cn(title && "mt-0.5 opacity-90")}>{children}</div>}
      </div>
      {action && <div className="shrink-0">{action}</div>}
    </div>
  );
}

/* ------------------------------------------------------------ PageHeader */
export function PageHeader({ title, description, actions, eyebrow }: {
  title: ReactNode; description?: ReactNode; actions?: ReactNode; eyebrow?: ReactNode;
}) {
  return (
    <header className="mb-6 flex flex-wrap items-end justify-between gap-4">
      <div className="min-w-0">
        {eyebrow && <p className="mb-1 text-xs font-semibold tracking-wider text-primary uppercase">{eyebrow}</p>}
        <h1 className="text-[22px] font-semibold tracking-tight text-foreground">{title}</h1>
        {description && <p className="mt-1 max-w-2xl text-[13px] leading-relaxed text-muted">{description}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </header>
  );
}

/* ------------------------------------------------------------ EmptyState */
export function EmptyState({ icon, title, description, action, className }: {
  icon?: ReactNode; title: ReactNode; description?: ReactNode; action?: ReactNode; className?: string;
}) {
  return (
    <div className={cn("flex flex-col items-center justify-center gap-2 px-6 py-12 text-center", className)}>
      {icon && <span className="mb-1 grid size-11 place-items-center rounded-xl bg-primary-soft text-primary [&_svg]:size-5">{icon}</span>}
      <p className="text-sm font-semibold text-foreground">{title}</p>
      {description && <p className="max-w-sm text-xs leading-relaxed text-muted">{description}</p>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}

/* -------------------------------------------------------------- Skeleton */
export function Skeleton({ className }: { className?: string }) {
  return <div className={cn("animate-pulse rounded-md bg-surface-2", className)} aria-hidden />;
}

/* ------------------------------------------------------------- StatCard */
export function Stat({ label, value, hint, icon }: { label: ReactNode; value: ReactNode; hint?: ReactNode; icon?: ReactNode }) {
  return (
    <Card className="flex items-start gap-3 p-4">
      {icon && <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-primary-soft text-primary [&_svg]:size-[18px]">{icon}</span>}
      <div className="min-w-0">
        <p className="text-xs font-medium text-muted">{label}</p>
        <p className="mt-0.5 truncate text-lg font-semibold tracking-tight text-foreground">{value}</p>
        {hint && <p className="truncate text-xs text-muted">{hint}</p>}
      </div>
    </Card>
  );
}

/* ----------------------------------------------------------------- Table */
export function Table({ className, ...props }: HTMLAttributes<HTMLTableElement>) {
  return (
    <div className="w-full min-w-0 overflow-x-auto">
      <table className={cn("w-full border-collapse text-left text-sm [&_td]:border-t [&_td]:border-border [&_td]:px-4 [&_td]:py-3.5 [&_td]:align-middle [&_th]:bg-surface-2 [&_th]:px-4 [&_th]:py-2.5 [&_th]:text-xs [&_th]:font-semibold [&_th]:tracking-wide [&_th]:text-muted [&_th]:uppercase [&_tbody_tr]:transition-colors [&_tbody_tr:hover]:bg-surface-2/60", className)} {...props} />
    </div>
  );
}
