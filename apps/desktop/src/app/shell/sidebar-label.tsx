import type * as React from 'react'

import { cn } from '@/lib/utils'

type SidebarPanelLabelProps = React.ComponentProps<'span'>

export function SidebarPanelLabel({ children, className, ...props }: SidebarPanelLabelProps) {
  return (
    <span
      className={cn(
        'flex min-w-0 items-center gap-1.5 text-[0.6875rem] font-medium text-sidebar-foreground/55',
        className
      )}
      {...props}
    >
      <span className="min-w-0 truncate leading-none">{children}</span>
    </span>
  )
}
