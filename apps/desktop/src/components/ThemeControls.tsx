/**
 * Leva-driven palette fine-tuning, dev-mode only.
 *
 * Two folders (`Theme / Light` and `Theme / Dark`) expose the seed colors
 * and mix percentages that drive the glass token derivation. Edits are live
 * only; use them to tune values before copying them back into presets/CSS.
 */

import { button, useControls } from 'leva'
import { useEffect, useMemo } from 'react'

import { getBaseColors, useTheme } from '@/themes/context'

interface ThemeTuningValues {
  accentFill: number
  accentSoft: string
  backgroundSeed: string
  bubbleMix: number
  bubbleSeed: string
  cardMix: number
  cardSeed: string
  chromeMix: number
  elevatedMix: number
  elevatedSeed: string
  foreground: string
  midground: string
  primary: string
  primaryFill: number
  primaryStroke: number
  quaternaryFill: number
  quaternaryStroke: number
  quinaryFill: number
  secondary: string
  secondaryFill: number
  secondaryStroke: number
  sidebarMix: number
  sidebarSeed: string
  tertiaryFill: number
  tertiaryStroke: number
  warm: string
}

const HEX_RE = /^#[0-9a-f]{6}$/i

const swatch = (value: string | undefined) =>
  typeof value === 'string' && HEX_RE.test(value.trim()) ? value : '#444444'

const pct = (value: number) => `${value}%`

const defaultsFor = (mode: 'light' | 'dark') => ({
  bubbleMix: mode === 'dark' ? 48 : 30,
  cardMix: mode === 'dark' ? 38 : 22,
  chromeMix: mode === 'dark' ? 36 : 44,
  elevatedMix: mode === 'dark' ? 46 : 28,
  primaryFill: 16,
  primaryStroke: 24,
  quaternaryFill: 5,
  quaternaryStroke: 6,
  quinaryFill: 3,
  secondaryFill: 11,
  secondaryStroke: 16,
  sidebarMix: mode === 'dark' ? 42 : 36,
  tertiaryFill: 8,
  tertiaryStroke: 10
})

const setCss = (name: string, value: string) => document.documentElement.style.setProperty(name, value)

function applyTuning(values: ThemeTuningValues) {
  setCss('--theme-foreground', values.foreground)
  setCss('--theme-primary', values.primary)
  setCss('--theme-secondary', values.secondary)
  setCss('--theme-accent-soft', values.accentSoft)
  setCss('--theme-midground', values.midground)
  setCss('--theme-warm', values.warm)
  setCss('--theme-background-seed', values.backgroundSeed)
  setCss('--theme-sidebar-seed', values.sidebarSeed)
  setCss('--theme-card-seed', values.cardSeed)
  setCss('--theme-elevated-seed', values.elevatedSeed)
  setCss('--theme-bubble-seed', values.bubbleSeed)
  setCss('--theme-mix-chrome', pct(values.chromeMix))
  setCss('--theme-mix-sidebar', pct(values.sidebarMix))
  setCss('--theme-mix-card', pct(values.cardMix))
  setCss('--theme-mix-elevated', pct(values.elevatedMix))
  setCss('--theme-mix-bubble', pct(values.bubbleMix))
  setCss('--theme-fill-primary-accent-mix', pct(values.primaryFill))
  setCss('--theme-fill-secondary-accent-mix', pct(values.secondaryFill))
  setCss('--theme-fill-tertiary-accent-mix', pct(values.tertiaryFill))
  setCss('--theme-fill-quaternary-accent-mix', pct(values.quaternaryFill))
  setCss('--theme-fill-quinary-accent-mix', pct(values.quinaryFill))
  setCss('--theme-stroke-primary-accent-mix', pct(values.primaryStroke))
  setCss('--theme-stroke-secondary-accent-mix', pct(values.secondaryStroke))
  setCss('--theme-stroke-tertiary-accent-mix', pct(values.tertiaryStroke))
  setCss('--theme-stroke-quaternary-accent-mix', pct(values.quaternaryStroke))
}

function buildSchema(skinName: string, mode: 'light' | 'dark') {
  const base = getBaseColors(skinName, mode)
  const mix = defaultsFor(mode)

  const schema = {
    foreground: { value: swatch(base.foreground), label: 'text base' },
    primary: { value: swatch(base.primary), label: 'primary' },
    secondary: { value: swatch(base.secondary), label: 'secondary' },
    accentSoft: { value: swatch(base.accent), label: 'accent soft' },
    midground: { value: swatch(base.midground ?? base.ring), label: 'midground' },
    warm: { value: swatch(base.primary), label: 'warm glow' },
    backgroundSeed: { value: swatch(base.background), label: 'chrome seed' },
    sidebarSeed: { value: swatch(base.sidebarBackground ?? base.background), label: 'sidebar seed' },
    cardSeed: { value: swatch(base.card), label: 'card seed' },
    elevatedSeed: { value: swatch(base.popover), label: 'elevated seed' },
    bubbleSeed: { value: swatch(base.userBubble ?? base.popover), label: 'bubble seed' },
    chromeMix: { value: mix.chromeMix, min: 0, max: 100, step: 1, label: 'chrome mix %' },
    sidebarMix: { value: mix.sidebarMix, min: 0, max: 100, step: 1, label: 'sidebar mix %' },
    cardMix: { value: mix.cardMix, min: 0, max: 100, step: 1, label: 'card mix %' },
    elevatedMix: { value: mix.elevatedMix, min: 0, max: 100, step: 1, label: 'elevated mix %' },
    bubbleMix: { value: mix.bubbleMix, min: 0, max: 100, step: 1, label: 'bubble mix %' },
    primaryFill: { value: mix.primaryFill, min: 0, max: 40, step: 1, label: 'fill primary %' },
    secondaryFill: { value: mix.secondaryFill, min: 0, max: 40, step: 1, label: 'fill secondary %' },
    tertiaryFill: { value: mix.tertiaryFill, min: 0, max: 40, step: 1, label: 'fill tertiary %' },
    quaternaryFill: { value: mix.quaternaryFill, min: 0, max: 40, step: 1, label: 'fill quaternary %' },
    quinaryFill: { value: mix.quinaryFill, min: 0, max: 40, step: 1, label: 'fill quinary %' },
    primaryStroke: { value: mix.primaryStroke, min: 0, max: 50, step: 1, label: 'stroke primary %' },
    secondaryStroke: { value: mix.secondaryStroke, min: 0, max: 50, step: 1, label: 'stroke secondary %' },
    tertiaryStroke: { value: mix.tertiaryStroke, min: 0, max: 50, step: 1, label: 'stroke tertiary %' },
    quaternaryStroke: { value: mix.quaternaryStroke, min: 0, max: 50, step: 1, label: 'stroke quaternary %' }
  }

  return {
    ...schema,
    'apply defaults': button(() => applyTuning(valuesFromSchema(schema)))
  } as Parameters<typeof useControls>[1]
}

function valuesFromSchema(schema: Record<string, { value: number | string }>): ThemeTuningValues {
  return Object.fromEntries(Object.entries(schema).map(([key, field]) => [key, field.value])) as unknown as ThemeTuningValues
}

/** Renders nothing — Leva's UI is a portal driven by `useControls`. */
export function ThemeControls() {
  const { resolvedMode, themeName } = useTheme()
  const light = useMemo(() => buildSchema(themeName, 'light'), [themeName])
  const dark = useMemo(() => buildSchema(themeName, 'dark'), [themeName])
  const lightValues = useControls('Theme / Light', light, { collapsed: resolvedMode !== 'light' }, [themeName])
  const darkValues = useControls('Theme / Dark', dark, { collapsed: resolvedMode !== 'dark' }, [themeName])

  useEffect(() => {
    applyTuning((resolvedMode === 'light' ? lightValues : darkValues) as ThemeTuningValues)
  }, [darkValues, lightValues, resolvedMode])

  return null
}
