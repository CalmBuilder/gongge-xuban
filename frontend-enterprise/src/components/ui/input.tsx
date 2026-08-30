import * as React from "react"

import { useI18n } from "@/i18n"
import { cn } from "@/lib/utils"

function Input({ className, type, ...props }: React.ComponentProps<"input">) {
  const { t } = useI18n()
  const localizedProps = {
    ...props,
    placeholder: typeof props.placeholder === "string" ? t(props.placeholder) : props.placeholder,
    title: typeof props.title === "string" ? t(props.title) : props.title,
    "aria-label": typeof props["aria-label"] === "string" ? t(props["aria-label"]) : props["aria-label"],
  }

  return (
    <input
      type={type}
      data-slot="input"
      className={cn(
        "h-8 w-full min-w-0 rounded-[var(--gg-radius-control)] border border-[var(--gg-border)] bg-transparent px-2.5 py-1 gg-type-control text-[var(--gg-ink)] transition-colors outline-none focus-visible:border-[var(--gg-cobalt)] focus-visible:ring-2 focus-visible:ring-[color-mix(in_srgb,var(--gg-cobalt)_16%,transparent)] file:inline-flex file:h-6 file:border-0 file:bg-transparent file:font-medium file:text-foreground placeholder:text-[var(--gg-slate)] disabled:pointer-events-none disabled:cursor-not-allowed disabled:bg-input/50 disabled:opacity-50 aria-invalid:border-destructive aria-invalid:ring-3 aria-invalid:ring-destructive/20",
        className
      )}
      {...localizedProps}
    />
  )
}

export { Input }
