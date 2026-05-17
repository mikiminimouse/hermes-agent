import { useStore } from '@nanostores/react'
import { useEffect, useMemo } from 'react'

import type { SetTitlebarToolGroup } from '@/app/shell/titlebar-controls'
import { Codicon } from '@/components/ui/codicon'
import { cn } from '@/lib/utils'
import {
  $rightRailActiveTabId,
  RIGHT_RAIL_PREVIEW_TAB_ID,
  type RightRailTabId,
  selectRightRailTab
} from '@/store/layout'
import {
  $filePreviewTabs,
  $previewReloadRequest,
  $previewTarget,
  closeActiveRightRailTab,
  closeRightRailTab,
  type PreviewTarget
} from '@/store/preview'

import { PreviewPane } from './preview-pane'

export const PREVIEW_RAIL_MIN_WIDTH = '18rem'
export const PREVIEW_RAIL_MAX_WIDTH = '38rem'

const INTRINSIC = `clamp(${PREVIEW_RAIL_MIN_WIDTH}, 36vw, 32rem)`

// Track for <Pane id="preview">. Folds the intrinsic clamp with a min-floor
// against --chat-min-width so the chat surface never gets squeezed below it.
// Subtracts the project browser width so preview yields rather than crushing
// the chat when both right-side panes are open.
export const PREVIEW_RAIL_PANE_WIDTH = `min(${INTRINSIC}, max(0rem, calc(100vw - var(--pane-chat-sidebar-width) - var(--pane-file-browser-width, 0rem) - var(--chat-min-width))))`

interface ChatPreviewRailProps {
  onRestartServer?: (url: string, context?: string) => Promise<string>
  setTitlebarToolGroup?: SetTitlebarToolGroup
}

interface RailTab {
  id: RightRailTabId
  label: string
  target: PreviewTarget
}

function tabLabelFor(target: PreviewTarget): string {
  const value = target.label || target.path || target.source || target.url
  const tail = value.split(/[\\/]/).filter(Boolean).at(-1)

  return tail || value || 'Preview'
}

export function ChatPreviewRail({ onRestartServer, setTitlebarToolGroup }: ChatPreviewRailProps) {
  const previewReloadRequest = useStore($previewReloadRequest)
  const activeTabId = useStore($rightRailActiveTabId)
  const filePreviewTabs = useStore($filePreviewTabs)
  const previewTarget = useStore($previewTarget)

  const tabs = useMemo<readonly RailTab[]>(
    () => [
      ...(previewTarget ? [{ id: RIGHT_RAIL_PREVIEW_TAB_ID, label: 'Preview', target: previewTarget } as RailTab] : []),
      ...filePreviewTabs.map(({ id, target }) => ({ id, label: tabLabelFor(target), target }) as RailTab)
    ],
    [filePreviewTabs, previewTarget]
  )

  const activeTab = tabs.find(tab => tab.id === activeTabId) ?? tabs[0]

  useEffect(() => {
    if (activeTab && activeTab.id !== activeTabId) {
      selectRightRailTab(activeTab.id)
    }
  }, [activeTab, activeTabId])

  if (!activeTab) {
    return null
  }

  const isPreview = activeTab.id === RIGHT_RAIL_PREVIEW_TAB_ID

  return (
    <aside className="relative flex h-full w-full min-w-0 flex-col overflow-hidden border-l border-(--ui-stroke-tertiary) bg-(--glass-editor-surface-background) text-(--ui-text-tertiary)">
      <div
        className="flex h-(--titlebar-height) shrink-0 overflow-x-auto overflow-y-hidden overscroll-x-contain border-b border-(--ui-stroke-tertiary) bg-(--glass-sidebar-surface-background) [-ms-overflow-style:none] [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
        role="tablist"
      >
        {tabs.map(tab => {
          const active = tab.id === activeTab.id

          return (
            <div
              className={cn(
                'group/tab relative flex h-full min-w-0 max-w-48 shrink-0 items-center text-[0.6875rem] font-medium [-webkit-app-region:no-drag]',
                active
                  ? 'bg-(--glass-editor-surface-background) text-foreground [--tab-bg:var(--glass-editor-surface-background)]'
                  : 'border-r border-(--ui-stroke-quaternary) text-(--ui-text-tertiary) [--tab-bg:var(--glass-sidebar-surface-background)] hover:bg-(--chrome-action-hover) hover:text-foreground hover:[--tab-bg:var(--chrome-action-hover)]'
              )}
              key={tab.id}
            >
              {active && <span aria-hidden="true" className="absolute inset-x-0 top-0 h-px bg-(--ui-stroke-primary)" />}
              <button
                aria-selected={active}
                className="flex h-full min-w-0 max-w-full items-center overflow-hidden pl-3 pr-2 text-left outline-none"
                onClick={() => selectRightRailTab(tab.id)}
                role="tab"
                title={tab.label}
                type="button"
              >
                <span className="block min-w-0 truncate">{tab.label}</span>
              </button>
              <span
                aria-hidden="true"
                className="pointer-events-none absolute inset-y-0 right-0 w-9 bg-[linear-gradient(to_right,transparent,var(--tab-bg)_55%)] opacity-0 transition-opacity group-hover/tab:opacity-100 group-focus-within/tab:opacity-100"
              />
              <button
                aria-label={`Close ${tab.label}`}
                className="pointer-events-none absolute right-1.5 top-1/2 grid size-4 -translate-y-1/2 place-items-center rounded-sm text-(--ui-text-tertiary) opacity-0 transition-[background-color,color,opacity] hover:bg-(--ui-bg-secondary) hover:text-foreground focus-visible:pointer-events-auto focus-visible:opacity-100 group-hover/tab:pointer-events-auto group-hover/tab:opacity-100 group-focus-within/tab:pointer-events-auto group-focus-within/tab:opacity-100"
                onClick={() => closeRightRailTab(tab.id)}
                title={`Close ${tab.label}`}
                type="button"
              >
                <Codicon name="close" size="0.75rem" />
              </button>
            </div>
          )
        })}
      </div>

      <div className="min-h-0 flex-1 overflow-hidden">
        <PreviewPane
          embedded
          onClose={closeActiveRightRailTab}
          onRestartServer={isPreview ? onRestartServer : undefined}
          reloadRequest={previewReloadRequest}
          setTitlebarToolGroup={setTitlebarToolGroup}
          target={activeTab.target}
        />
      </div>
    </aside>
  )
}
