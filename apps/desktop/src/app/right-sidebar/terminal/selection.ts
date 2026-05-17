import type { Terminal } from '@xterm/xterm'
import type { CSSProperties } from 'react'

export const TERMINAL_THEME = {
  background: '#00000000',
  cursor: '#6f6f6f',
  cursorAccent: '#f7f7f7',
  foreground: '#4d4d4d',
  selectionBackground: '#8c8c8c33'
}

export const isMacPlatform = () => navigator.platform.toLowerCase().includes('mac')

export const addSelectionShortcutLabel = () => (isMacPlatform() ? '⌘L' : 'Ctrl+L')

export function isAddSelectionShortcut(event: KeyboardEvent) {
  return isMacPlatform()
    ? event.metaKey && !event.shiftKey && event.key.toLowerCase() === 'l'
    : event.ctrlKey && !event.shiftKey && event.key.toLowerCase() === 'l'
}

function selectionLineCount(text: string) {
  return Math.max(1, text.trim().split(/\r?\n/).length)
}

export function terminalSelectionLabel(term: Terminal, shellName: string, text: string) {
  const position = term.getSelectionPosition()

  if (position) {
    return position.start.y === position.end.y
      ? `${shellName}:${position.start.y}`
      : `${shellName}:${position.start.y}-${position.end.y}`
  }

  const lines = selectionLineCount(text)

  return `${shellName}:${lines} line${lines === 1 ? '' : 's'}`
}

export function terminalSelectionAnchor(host: HTMLDivElement): CSSProperties | null {
  const selectionRects = Array.from(host.querySelectorAll<HTMLElement>('.xterm-selection div'))
    .map(node => node.getBoundingClientRect())
    .filter(rect => rect.width > 0 && rect.height > 0)

  const rect = selectionRects.at(-1)

  if (!rect) {
    return null
  }

  const hostRect = host.getBoundingClientRect()
  const buttonWidth = 128
  const left = Math.min(Math.max(rect.left - hostRect.left, 8), Math.max(8, host.clientWidth - buttonWidth - 8))
  const top = Math.min(Math.max(rect.bottom - hostRect.top + 4, 8), Math.max(8, host.clientHeight - 34))

  return { left, top }
}
