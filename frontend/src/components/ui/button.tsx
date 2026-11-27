import { forwardRef, type ButtonHTMLAttributes } from "react";
import { Slot } from "radix-ui";
import { cva, type VariantProps } from "class-variance-authority";
import { Loader2 } from "lucide-react";
import { cn } from "../../lib/utils";

export const buttonVariants = cva(
  "inline-flex shrink-0 cursor-pointer items-center justify-center gap-2 whitespace-nowrap rounded-md text-[13px] font-medium " +
    "transition-[color,background-color,border-color,box-shadow] outline-none select-none " +
    "focus-visible:ring-[3px] focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50 " +
    "[&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4",
  {
    variants: {
      variant: {
        primary: "bg-primary text-primary-foreground shadow-xs hover:bg-primary-hover",
        secondary: "border border-border bg-surface text-foreground shadow-xs hover:bg-surface-2 hover:border-border-strong",
        ghost: "text-foreground-2 hover:bg-surface-2 hover:text-foreground",
        soft: "bg-primary-soft text-primary hover:bg-primary/15",
        danger: "bg-danger text-white shadow-xs hover:bg-danger/90",
        "danger-ghost": "text-danger-text hover:bg-danger-soft",
        link: "h-auto px-0 text-primary underline-offset-4 hover:underline",
      },
      size: {
        sm: "h-8 px-3 text-xs",
        md: "h-9 px-3.5",
        lg: "h-10 px-5 text-sm",
        icon: "size-9",
        "icon-sm": "size-8",
        "icon-xs": "size-7 rounded-md",
      },
    },
    defaultVariants: { variant: "secondary", size: "md" },
  },
);

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement>, VariantProps<typeof buttonVariants> {
  asChild?: boolean;
  loading?: boolean;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { className, variant, size, asChild = false, loading = false, disabled, children, type, ...props },
  ref,
) {
  const Comp = asChild ? Slot.Root : "button";
  return (
    <Comp
      ref={ref}
      type={asChild ? undefined : (type ?? "button")}
      className={cn(buttonVariants({ variant, size }), className)}
      disabled={asChild ? undefined : disabled || loading}
      aria-busy={loading || undefined}
      {...props}
    >
      {asChild ? children : (<>{loading && <Loader2 className="animate-spin" />}{children}</>)}
    </Comp>
  );
});
