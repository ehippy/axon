import type { Register, SessionRateLimit } from 'claude-code'

// Claude passes usage to function hooks on every surface; command hooks never see
// it. Hand the documented status-line shape to the bundled collector, which
// validates, dedupes and queues it like any other Axon report.
function rateLimits(limits: readonly SessionRateLimit[]) {
  const windows: Record<string, { used_percentage: number; resets_at: number | null }> = {}
  for (const limit of limits) {
    if (limit.kind !== 'five_hour' && limit.kind !== 'seven_day') continue
    const resets = limit.resetsAt ? Date.parse(limit.resetsAt) / 1000 : NaN
    windows[limit.kind] = {
      used_percentage: Math.min(100, Math.max(0, limit.percentUsed)),
      resets_at: Number.isFinite(resets) ? resets : null,
    }
  }
  return Object.keys(windows).length ? JSON.stringify({ rate_limits: windows }) : undefined
}

export const register: Register = on => {
  on('session.measure', async ($, e, next) => {
    const stdin = rateLimits(e.rateLimits)
    if (stdin) {
      try {
        await $.process.run([$.plugin.root + '/bin/collect'], { stdin, timeoutMs: 10_000 })
      } catch {
        // Observer failures never reach the session.
      }
    }
    return next(e)
  })
}
