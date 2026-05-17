/**
 * Desktop theme context.
 *
 * Applies the active theme as CSS custom properties on :root so every
 * Tailwind utility that references a color or font-family token picks up
 * the change automatically.
 *
 * Mode (light/dark/system) controls brightness; skin controls accent.
 * The two are persisted independently. Shift+X toggles light/dark.
 */

import { createContext, type ReactNode, useCallback, useContext, useEffect, useMemo, useState } from 'react'

import { matchesQuery, useMediaQuery } from '@/hooks/use-media-query'

import { BUILTIN_THEME_LIST, BUILTIN_THEMES, DEFAULT_SKIN_NAME, DEFAULT_TYPOGRAPHY, nousTheme } from './presets'
import type { DesktopTheme, DesktopThemeColors } from './types'

const SKIN_KEY = 'hermes-desktop-theme-v2'
const MODE_KEY = 'hermes-desktop-mode-v1'
const RETIRED_SKINS = new Set(['nous-light', 'default', 'gold'])

export type ThemeMode = 'light' | 'dark' | 'system'

const INJECTED_FONT_URLS = new Set<string>()

const resolveMode = (mode: ThemeMode, systemDark = matchesQuery('(prefers-color-scheme: dark)')): 'light' | 'dark' =>
  mode === 'system' ? (systemDark ? 'dark' : 'light') : mode

const normalizeSkin = (name: string | null | undefined): string =>
  name && BUILTIN_THEMES[name] && !RETIRED_SKINS.has(name) ? name : DEFAULT_SKIN_NAME

// ─── Color math (for synthesised light variants of dark-only skins) ────────

function hexToRgb(hex: string): [number, number, number] | null {
  const clean = hex.trim().replace(/^#/, '')

  if (!/^[0-9a-f]{6}$/i.test(clean)) {
    return null
  }

  return [0, 2, 4].map(i => parseInt(clean.slice(i, i + 2), 16)) as [number, number, number]
}

const rgbToHex = ([r, g, b]: [number, number, number]) =>
  `#${[r, g, b].map(n => Math.round(n).toString(16).padStart(2, '0')).join('')}`

function mix(a: string, b: string, amount: number): string {
  const ar = hexToRgb(a)
  const br = hexToRgb(b)

  return ar && br
    ? rgbToHex([ar[0] + (br[0] - ar[0]) * amount, ar[1] + (br[1] - ar[1]) * amount, ar[2] + (br[2] - ar[2]) * amount])
    : a
}

function readableOn(hex: string): string {
  const rgb = hexToRgb(hex)

  if (!rgb) {
    return '#ffffff'
  }

  const [r, g, b] = rgb.map(v => {
    const c = v / 255

    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
  })

  return 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.58 ? '#161616' : '#ffffff'
}

function synthLightColors(seed: DesktopTheme): DesktopThemeColors {
  const accent = seed.colors.ring || seed.colors.primary
  const soft = mix('#ffffff', accent, 0.1)
  const softer = mix('#ffffff', accent, 0.06)
  const border = mix('#ececef', accent, 0.14)
  const midground = seed.colors.midground ?? accent

  return {
    background: '#ffffff',
    foreground: '#161616',
    card: '#ffffff',
    cardForeground: '#161616',
    muted: softer,
    mutedForeground: mix('#6b6b70', accent, 0.16),
    popover: '#ffffff',
    popoverForeground: '#161616',
    primary: accent,
    primaryForeground: readableOn(accent),
    secondary: soft,
    secondaryForeground: mix('#2a2a2a', accent, 0.34),
    accent: soft,
    accentForeground: mix('#2a2a2a', accent, 0.34),
    border,
    input: mix('#e2e2e6', accent, 0.18),
    ring: accent,
    midground,
    midgroundForeground: readableOn(midground),
    destructive: '#b94a3a',
    destructiveForeground: '#ffffff',
    sidebarBackground: mix('#fafafa', accent, 0.05),
    sidebarBorder: border,
    userBubble: soft,
    userBubbleBorder: border
  }
}

/** Returns the seed palette for a given skin + mode (no overrides applied). */
export function getBaseColors(skinName: string, mode: 'light' | 'dark'): DesktopThemeColors {
  const seed = BUILTIN_THEMES[skinName] ?? nousTheme

  if (mode === 'dark') {
    return seed.darkColors ?? seed.colors
  }

  return seed.darkColors ? seed.colors : synthLightColors(seed)
}

function deriveTheme(skinName: string, mode: 'light' | 'dark'): DesktopTheme {
  const seed = BUILTIN_THEMES[skinName] ?? nousTheme

  return {
    ...seed,
    name: `${skinName}-${mode}`,
    label: `${seed.label} ${mode === 'light' ? 'Light' : 'Dark'}`,
    description: `${seed.label} ${mode} palette`,
    colors: getBaseColors(skinName, mode)
  }
}

/**
 * Some palettes intentionally keep a bright background even when
 * `mode === 'dark'`, so we shouldn't apply the `.dark` class. Decide from
 * the actual background luminance.
 */
function renderedModeFor(colors: DesktopThemeColors, mode: 'light' | 'dark'): 'light' | 'dark' {
  const rgb = hexToRgb(colors.background)

  if (!rgb) {
    return mode
  }

  const [r, g, b] = rgb.map(v => v / 255)

  return 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.5 ? 'light' : 'dark'
}

// ─── CSS application ────────────────────────────────────────────────────────

function applyTheme(theme: DesktopTheme, mode: 'light' | 'dark') {
  if (typeof document === 'undefined') {
    return
  }

  const root = document.documentElement
  const c = theme.colors
  const typo = { ...DEFAULT_TYPOGRAPHY, ...nousTheme.typography, ...theme.typography }
  const rendered = renderedModeFor(c, mode)
  const midground = c.midground ?? c.ring
  const skinName = theme.name.endsWith(`-${mode}`) ? theme.name.slice(0, -mode.length - 1) : theme.name
  const neutralChrome = rendered === 'dark' ? '#0d0d0e' : '#f3f3f3'
  const neutralSidebar = rendered === 'dark' ? '#0a0a0b' : '#f3f3f3'
  const neutralCard = rendered === 'dark' ? '#161618' : '#fcfcfc'
  const chromeMix = rendered === 'dark' ? 36 : 44
  const sidebarMix = rendered === 'dark' ? 42 : 36
  const cardMix = rendered === 'dark' ? 38 : 22
  const elevatedMix = rendered === 'dark' ? 46 : 28
  const bubbleMix = rendered === 'dark' ? 48 : 30

  root.style.setProperty('color-scheme', rendered)
  root.dataset.hermesTheme = skinName
  root.dataset.hermesMode = rendered
  root.classList.toggle('dark', rendered === 'dark')

  const set = (k: string, v: string) => root.style.setProperty(k, v)
  set('--theme-foreground', c.foreground)
  set('--theme-primary', c.primary)
  set('--theme-secondary', c.secondary)
  set('--theme-accent-soft', c.accent)
  set('--theme-midground', midground)
  set('--theme-warm', c.primary)
  set('--theme-background-seed', c.background)
  set('--theme-sidebar-seed', c.sidebarBackground ?? c.background)
  set('--theme-card-seed', c.card)
  set('--theme-elevated-seed', c.popover)
  set('--theme-bubble-seed', c.userBubble ?? c.popover)
  set('--theme-neutral-chrome', neutralChrome)
  set('--theme-neutral-sidebar', neutralSidebar)
  set('--theme-neutral-card', neutralCard)
  set('--theme-mix-chrome', `${chromeMix}%`)
  set('--theme-mix-sidebar', `${sidebarMix}%`)
  set('--theme-mix-card', `${cardMix}%`)
  set('--theme-mix-elevated', `${elevatedMix}%`)
  set('--theme-mix-bubble', `${bubbleMix}%`)
  set('--theme-fill-primary-accent-mix', '16%')
  set('--theme-fill-secondary-accent-mix', '11%')
  set('--theme-fill-tertiary-accent-mix', '8%')
  set('--theme-fill-quaternary-accent-mix', '5%')
  set('--theme-fill-quinary-accent-mix', '3%')
  set('--theme-stroke-primary-accent-mix', '24%')
  set('--theme-stroke-secondary-accent-mix', '16%')
  set('--theme-stroke-tertiary-accent-mix', '10%')
  set('--theme-stroke-quaternary-accent-mix', '6%')
  set('--ui-base', 'var(--theme-foreground)')
  set('--ui-accent', 'var(--theme-midground)')
  set('--ui-accent-secondary', 'var(--theme-primary)')
  set('--ui-warm', 'var(--theme-warm)')
  set('--ui-bg-chrome', 'color-mix(in srgb, var(--theme-background-seed) var(--theme-mix-chrome), var(--theme-neutral-chrome))')
  set('--ui-bg-sidebar', 'color-mix(in srgb, var(--theme-sidebar-seed) var(--theme-mix-sidebar), var(--theme-neutral-sidebar))')
  set('--ui-bg-editor', 'color-mix(in srgb, var(--theme-card-seed) var(--theme-mix-card), var(--theme-neutral-card))')
  set('--ui-bg-elevated', 'color-mix(in srgb, var(--theme-elevated-seed) var(--theme-mix-elevated), var(--theme-neutral-card))')
  set('--ui-bg-input', 'var(--ui-bg-editor)')
  set('--glass-surface-background', 'var(--ui-bg-editor)')
  set('--glass-sidebar-surface-background', 'var(--ui-bg-sidebar)')
  set('--glass-chat-surface-background', 'var(--ui-bg-chrome)')
  set('--glass-editor-surface-background', 'var(--ui-bg-chrome)')
  set('--glass-chat-bubble-background', 'color-mix(in srgb, var(--theme-bubble-seed) var(--theme-mix-bubble), var(--theme-neutral-card))')
  set('--glass-chat-bubble-opaque-background', 'var(--ui-bg-editor)')
  set('--dt-background', 'var(--ui-bg-chrome)')
  set('--dt-foreground', 'var(--ui-text-primary)')
  set('--dt-card', 'var(--ui-bg-editor)')
  set('--dt-card-foreground', 'var(--ui-text-primary)')
  set('--dt-muted', c.muted)
  set('--dt-muted-foreground', 'var(--ui-text-tertiary)')
  set('--dt-popover', 'var(--ui-bg-elevated)')
  set('--dt-popover-foreground', 'var(--ui-text-primary)')
  set('--dt-primary', 'var(--theme-primary)')
  set('--dt-primary-foreground', c.primaryForeground)
  set('--dt-secondary', 'var(--theme-secondary)')
  set('--dt-secondary-foreground', c.secondaryForeground)
  set('--dt-accent', 'var(--theme-accent-soft)')
  set('--dt-accent-foreground', c.accentForeground)
  set('--dt-border', c.border)
  set('--dt-input', c.input)
  set('--dt-ring', c.ring)
  set('--dt-midground', 'var(--theme-midground)')
  set('--dt-midground-foreground', c.midgroundForeground ?? readableOn(midground))
  set('--dt-composer-ring', c.composerRing ?? midground)
  set('--dt-destructive', c.destructive)
  set('--dt-destructive-foreground', c.destructiveForeground)
  set('--dt-sidebar-bg', 'var(--ui-bg-sidebar)')
  set('--dt-sidebar-border', c.sidebarBorder ?? c.border)
  set('--dt-user-bubble', 'var(--glass-chat-bubble-background)')
  set('--dt-user-bubble-border', c.userBubbleBorder ?? c.border)
  set('--dt-font-sans', typo.fontSans)
  set('--dt-font-mono', typo.fontMono)
  set('--noise-opacity-mul', rendered === 'dark' ? 'calc(0.04 / 0.21)' : 'calc(0.34 / 0.21)')
  window.hermesDesktop?.setTitleBarTheme?.({
    background: c.background,
    foreground: c.foreground
  })

  if (typo.fontUrl && !INJECTED_FONT_URLS.has(typo.fontUrl)) {
    const link = document.createElement('link')
    link.rel = 'stylesheet'
    link.href = typo.fontUrl
    link.dataset.hermesThemeFont = 'true'
    document.head.appendChild(link)
    INJECTED_FONT_URLS.add(typo.fontUrl)
  }
}

// Boot-time paint to avoid a flash before <ThemeProvider> mounts.
if (typeof window !== 'undefined') {
  const skin = normalizeSkin(window.localStorage.getItem(SKIN_KEY))
  const mode = (window.localStorage.getItem(MODE_KEY) as ThemeMode) ?? 'light'
  const resolved = resolveMode(mode)
  applyTheme(deriveTheme(skin, resolved), resolved)
}

// ─── Context ────────────────────────────────────────────────────────────────

interface ThemeContextValue {
  theme: DesktopTheme
  themeName: string
  mode: ThemeMode
  resolvedMode: 'light' | 'dark'
  availableThemes: Array<{ name: string; label: string; description: string }>
  setTheme: (name: string) => void
  setMode: (mode: ThemeMode) => void
}

const SKIN_LIST = BUILTIN_THEME_LIST.map(({ name, label, description }) => ({ name, label, description }))

const ThemeContext = createContext<ThemeContextValue>({
  theme: nousTheme,
  themeName: DEFAULT_SKIN_NAME,
  mode: 'light',
  resolvedMode: 'light',
  availableThemes: SKIN_LIST,
  setTheme: () => {},
  setMode: () => {}
})

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [themeName, setThemeNameState] = useState(() =>
    typeof window === 'undefined' ? DEFAULT_SKIN_NAME : normalizeSkin(window.localStorage.getItem(SKIN_KEY))
  )

  const [mode, setModeState] = useState<ThemeMode>(() =>
    typeof window === 'undefined' ? 'light' : ((window.localStorage.getItem(MODE_KEY) as ThemeMode) ?? 'light')
  )

  const systemDark = useMediaQuery('(prefers-color-scheme: dark)')
  const resolvedMode = resolveMode(mode, systemDark)
  const activeTheme = useMemo(() => deriveTheme(themeName, resolvedMode), [themeName, resolvedMode])

  useEffect(() => applyTheme(activeTheme, resolvedMode), [activeTheme, resolvedMode])

  const setTheme = useCallback((name: string) => {
    const next = normalizeSkin(name)
    setThemeNameState(next)
    window.localStorage.setItem(SKIN_KEY, next)
  }, [])

  const setMode = useCallback((next: ThemeMode) => {
    setModeState(next)
    window.localStorage.setItem(MODE_KEY, next)
  }, [])

  // Shift+X toggles light/dark anywhere outside an editable field.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const t = event.target as HTMLElement | null

      const editing =
        t?.isContentEditable ||
        t instanceof HTMLInputElement ||
        t instanceof HTMLTextAreaElement ||
        t instanceof HTMLSelectElement

      if (editing || event.repeat || event.altKey || event.ctrlKey || event.metaKey) {
        return
      }

      if (event.shiftKey && event.code === 'KeyX') {
        setMode(resolvedMode === 'dark' ? 'light' : 'dark')
      }
    }

    window.addEventListener('keydown', onKeyDown)

    return () => window.removeEventListener('keydown', onKeyDown)
  }, [resolvedMode, setMode])

  const value = useMemo<ThemeContextValue>(
    () => ({ theme: activeTheme, themeName, mode, resolvedMode, availableThemes: SKIN_LIST, setTheme, setMode }),
    [activeTheme, themeName, mode, resolvedMode, setTheme, setMode]
  )

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}

export const useTheme = (): ThemeContextValue => useContext(ThemeContext)

/** Sync the desktop skin with the active Hermes backend theme on connect. */
export function useSyncThemeFromBackend(backendThemeName: string | undefined, setTheme: (name: string) => void) {
  useEffect(() => {
    if (backendThemeName && BUILTIN_THEMES[backendThemeName]) {
      setTheme(backendThemeName)
    }
  }, [backendThemeName, setTheme])
}
