import * as React from "react";
import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

/**
 * Buttons are quiet by default: a hairline border and no fill, so the accent
 * colour stays reserved for state. `accent` is the one filled variant.
 */
const buttonVariants = cva(
  "inline-flex items-center justify-center gap-1.5 whitespace-nowrap rounded-[var(--radius-cell)] font-medium transition-colors duration-[var(--duration-feedback)] ease-[var(--ease-feedback)] disabled:pointer-events-none disabled:opacity-40 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-accent [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        ghost: "text-ink-muted hover:bg-elevated hover:text-ink",
        outline: "border border-line bg-transparent text-ink hover:bg-elevated",
        accent:
          "bg-accent text-accent-ink hover:bg-accent/90 disabled:bg-elevated disabled:text-ink-dim",
        danger: "border border-line text-danger hover:bg-danger/10",
      },
      size: {
        sm: "h-6 px-2 text-2xs [&_svg]:size-3",
        md: "h-7 px-2.5 text-xs [&_svg]:size-3.5",
        icon: "size-7 [&_svg]:size-3.5",
        iconSm: "size-6 [&_svg]:size-3",
      },
    },
    defaultVariants: { variant: "ghost", size: "md" },
  },
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean;
}

export function Button({
  className,
  variant,
  size,
  asChild = false,
  ...props
}: ButtonProps): React.ReactElement {
  const Component = asChild ? Slot : "button";
  return (
    <Component className={cn(buttonVariants({ variant, size }), className)} {...props} />
  );
}

export { buttonVariants };
