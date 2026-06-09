/**
 * test/lib/effect.ts — recovers `it.effect` ergonomics on plain `bun test`
 * WITHOUT @effect/vitest (spec v4 §5 Layer 1). Each test gets a per-call
 * ManagedRuntime so layers/services don't leak between tests; the TestClock
 * is installed via `TestClock.layer()` (beta.78 replacement for 3.x
 * `TestContext.layer()` — there is no `TestContext` on this line).
 *
 * Gotcha (verified against effect@4.0.0-beta.78 .d.ts): `TestClock.adjust`/
 * `setTime` return `Effect<void>` with no R requirement, so a program that only
 * uses them won't surface TestClock in its R channel — hence the
 * `R extends TestClock.TestClock` constraint to force the layer to be provided.
 */
import { type Effect, Layer, ManagedRuntime } from 'effect'
import { TestClock } from 'effect/testing'

export const TestClockLayer: Layer.Layer<TestClock.TestClock> = TestClock.layer()

/** Run an Effect with the test clock available; fresh runtime per call. */
export async function testEffect<A, E, R extends TestClock.TestClock>(effect: Effect.Effect<A, E, R>): Promise<A> {
  const runtime = ManagedRuntime.make(TestClockLayer)
  try {
    return await runtime.runPromise(effect as Effect.Effect<A, E, TestClock.TestClock>)
  } finally {
    await runtime.dispose()
  }
}

/** Run an Effect against a provided layer (+ test clock), fresh runtime per call. */
export async function testLayer<A, E, ROut, RErr>(
  layer: Layer.Layer<ROut, RErr, never>,
  effect: Effect.Effect<A, E, ROut | TestClock.TestClock>
): Promise<A> {
  const full = Layer.mergeAll(TestClock.layer(), layer)
  const runtime = ManagedRuntime.make(full)
  try {
    return await runtime.runPromise(effect)
  } finally {
    await runtime.dispose()
  }
}

export { TestClock }
