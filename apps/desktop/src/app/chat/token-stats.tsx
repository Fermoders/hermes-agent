import { useAuiState } from '@assistant-ui/react'
import type { TokenTotals, Usage } from '@hermes/shared'
import { useStore } from '@nanostores/react'
import { useEffect, useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { useI18n } from '@/i18n'
import { $gateway } from '@/store/gateway'
import { $sessionStates, publishSessionState, requestForOwnedSession } from '@/store/session-states'
import { ambientRequestFor } from '@/store/session-gone-latch'
import { useSessionView } from './session-view'

export function TokenTotalsLine({ label, totals }: { label: string; totals: TokenTotals }) {
  const { t } = useI18n()
  const copy = t.tokenStats
  const n = (value: number | null | undefined) => value == null ? '—' : value.toLocaleString()
  return <div className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-(--ui-text-tertiary)">
    <span>{label}: {n(totals.total)}</span>
    <span>{copy.read} {n(totals.input)}</span>
    <span>{copy.cacheRead} {n(totals.cache_read)}</span>
    <span>{copy.cacheWrite} {n(totals.cache_write)}</span>
    <span>{copy.output} {n(totals.output)}</span>
    <span>{totals.tokens_per_second == null ? '—' : totals.tokens_per_second.toFixed(1)} {copy.rate}</span>
    <span>{copy.agents} {totals.delegated_calls}</span>
    {(totals.missing_calls ?? 0) > 0 && <span>{copy.partial} {totals.missing_calls}</span>}
  </div>
}

function publishUsage(sid: string, usage: Usage) {
  const state = $sessionStates.get()[sid]
  if (!state || !usage.token_stats) return
  const base = state.usage ?? (typeof usage.calls === 'number' && typeof usage.input === 'number' &&
    typeof usage.output === 'number' && typeof usage.total === 'number'
    ? { calls: usage.calls, input: usage.input, output: usage.output, total: usage.total } : null)
  if (base) publishSessionState(sid, { ...state, usage: { ...base, token_stats: usage.token_stats } })
}

function useTokenStats(poll = false) {
  const sid = useStore(useSessionView().$runtimeId)
  const states = useStore($sessionStates)
  const reads = useRef({ generation: 0, resetting: false })
  const gateway = useStore($gateway)
  useEffect(() => {
    if (!poll || !sid || !gateway) return
    let disposed = false
    const refresh = async () => {
      if (reads.current.resetting) return
      const generation = ++reads.current.generation
      const previousUsage = $sessionStates.get()[sid]?.usage
      try {
        const usage = await requestForOwnedSession<Usage>(sid, ambientRequestFor(gateway), 'session.usage', { session_id: sid })
        if (!disposed && generation === reads.current.generation &&
            $sessionStates.get()[sid]?.usage === previousUsage) publishUsage(sid, usage)
      } catch { /* An older/offline backend is unavailable, never synthetic zero. */ }
    }
    void refresh()
    const timer = setInterval(() => void refresh(), 3000)
    return () => { disposed = true; clearInterval(timer) }
  }, [sid, poll, gateway])
  return { sid, stats: sid ? states[sid]?.usage?.token_stats : undefined, reads }
}

export function ChatTokenPanel() {
  const { sid, stats, reads } = useTokenStats(true)
  const { t } = useI18n()
  const busy = useStore(useSessionView().$busy)
  const [error, setError] = useState<string | null>(null)
  const reset = async () => {
    const gateway = $gateway.get()
    if (!sid || !gateway || reads.current.resetting) return
    const generation = ++reads.current.generation
    const previousUsage = $sessionStates.get()[sid]?.usage
    reads.current.resetting = true
    try {
      const usage = await requestForOwnedSession<Usage>(sid, ambientRequestFor(gateway), 'session.tokens.new_task', { session_id: sid })
      if (generation === reads.current.generation &&
          $sessionStates.get()[sid]?.usage === previousUsage) publishUsage(sid, usage)
      setError(null)
    } catch (e) { setError(String(e)) }
    finally { reads.current.resetting = false }
  }
  return <section aria-label={t.tokenStats.title} className="flex flex-col gap-1 px-3 py-1">
    {stats ? <><TokenTotalsLine label={t.tokenStats.task} totals={stats.task} /><TokenTotalsLine label={t.tokenStats.session} totals={stats.session} /></> : <span className="text-xs text-(--ui-text-tertiary)">{t.tokenStats.unavailable}</span>}
    <div><Button disabled={!sid || busy} onClick={() => void reset()} size="micro" variant="text">{t.tokenStats.newTask}</Button></div>
    <span className="text-xs text-(--ui-text-tertiary)">{t.tokenStats.rateHint}</span>
    {error && <span role="alert">{error}</span>}
  </section>
}

export function ResponseTokenFooter() {
  const rowId = useAuiState(s => (s.message.metadata?.custom?.finalAssistantRowId ?? s.message.metadata?.custom?.rowId) as number | undefined)
  const complete = useAuiState(s => s.message.status?.type === 'complete')
  const { stats } = useTokenStats()
  const { t } = useI18n()
  const totals = rowId == null ? undefined : stats?.responses[String(rowId)]
  if (!complete || !totals) return null
  return <TokenTotalsLine label={t.tokenStats.response} totals={totals} />
}
