import { useEffect, useMemo, useState } from 'react'

import { useI18n } from '@/i18n'

const REMOTE_PROVIDER = 'test'

interface ProviderRuntime {
  api_key?: string
  base_url?: string
}

interface TokenTotals {
  charged_input_tokens?: number
  charged_output_tokens?: number
  charged_total_tokens?: number
  input_tokens?: number
  output_tokens?: number
  total_tokens?: number
}

interface ModelUsage {
  requests?: number
  tokens?: TokenTotals
}

interface RemoteUsage {
  by_model?: Record<string, ModelUsage>
  failed_requests?: number
  name?: string
  requests?: number
  token_limit?: number
  tokens?: TokenTotals
}

interface LimitItem {
  color?: string
  detail?: string
  key: string
  label?: string
  percent?: number
  provider: string
  time_color?: string
  time_progress?: number | null
  trailing_text?: string
}

interface RemoteLimitWindow {
  detail?: string
  elapsed_percent?: number
  key?: string
  label?: string
  remaining_seconds?: number
  remaining_text?: string
  used_percent?: number
}

interface RemoteLimitModel {
  windows?: RemoteLimitWindow[] | null
}

interface RemoteLimitsPayload {
  models?: Record<string, RemoteLimitModel>
}

interface AiLimitsPanelProps {
  requestGateway: <T>(method: string, params?: Record<string, unknown>) => Promise<T>
}

interface TokenSample {
  timestamp: number
  total: number
}

function numeric(value: number | undefined): number {
  return Number.isFinite(value) ? Number(value) : 0
}

function clampPercent(value: number | null | undefined): number {
  return Math.max(0, Math.min(100, numeric(value ?? undefined)))
}

function chargedTotal(tokens: TokenTotals | undefined): number {
  return numeric(tokens?.charged_total_tokens ?? tokens?.total_tokens)
}

function chargedInput(tokens: TokenTotals | undefined): number {
  return numeric(tokens?.charged_input_tokens ?? tokens?.input_tokens)
}

function chargedOutput(tokens: TokenTotals | undefined): number {
  return numeric(tokens?.charged_output_tokens ?? tokens?.output_tokens)
}

function formatTokens(value: number): string {
  return new Intl.NumberFormat('ru-RU', {
    notation: value >= 100_000 ? 'compact' : 'standard',
    maximumFractionDigits: 1
  }).format(value)
}

function formatTokenRate(value: number): string {
  return new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 }).format(value)
}

function limitLabel(item: LimitItem): string {
  if (item.provider === 'deepseek') {
    const total = item.detail?.match(/\$[\d.,]+/)?.[0]

    return `DeepSeek API USD${total ? ` ${total} total` : ''}`
  }

  if (item.provider === 'chatgpt' && item.label === '7д') {
    return 'GPT 7д'
  }

  return item.label || item.provider
}

function limitDetail(windowInfo: RemoteLimitWindow): string {
  const detail = String(windowInfo.detail || '').trim()
  if (detail) return detail

  return `${Math.round(numeric(windowInfo.used_percent))}%/${Math.round(numeric(windowInfo.elapsed_percent))}%`
}

function limitRemaining(windowInfo: RemoteLimitWindow): string {
  const supplied = String(windowInfo.remaining_text || '').trim()
  if (supplied) return supplied

  const seconds = numeric(windowInfo.remaining_seconds)
  return seconds > 0 ? `${Math.max(1, Math.ceil(seconds / 3_600))}ч` : ''
}

function mapRemoteLimits(payload: RemoteLimitsPayload): LimitItem[] {
  const models = payload.models || {}
  const rows: LimitItem[] = []

  const append = (model: 'gpt' | 'gpt_codex_spark', fallbackLabel: string, color: string) => {
    const windows = models[model]?.windows
    const windowInfo = Array.isArray(windows) ? windows[0] : null
    if (!windowInfo) return

    rows.push({
      provider: 'chatgpt',
      key: String(windowInfo.key || model),
      label: String(windowInfo.label || fallbackLabel),
      detail: limitDetail(windowInfo),
      percent: numeric(windowInfo.used_percent),
      time_progress: numeric(windowInfo.elapsed_percent),
      trailing_text: limitRemaining(windowInfo),
      color,
      time_color: '#bfdbfe'
    })
  }

  append('gpt', '7д', '#9333ea')
  append('gpt_codex_spark', 'GPT-5.3-Codex-Spark ChatGPT 7д', '#60a5fa')
  return rows
}

function LimitBar({ item }: { item: LimitItem }) {
  const percent = clampPercent(item.percent)
  const timeProgress = item.time_progress == null ? null : clampPercent(item.time_progress)

  return (
    <div className="relative mt-1 h-5 w-full overflow-hidden rounded-full bg-(--ui-control-background)">
      <div
        className="absolute inset-y-0 left-0"
        style={{ background: item.color || '#5865f2', width: `${percent}%` }}
      />
      {timeProgress != null && timeProgress > 0 ? (
        <div
          className="absolute bottom-0.5 left-0.5 h-0.75 rounded-full"
          style={{ background: item.time_color || '#bfdbfe', width: `calc(${timeProgress}% - 4px)` }}
        />
      ) : null}
      <div className="absolute inset-0 flex min-w-0 items-center px-2 text-[10px] font-semibold text-white [text-shadow:0_1px_0_rgba(0,0,0,0.35)]">
        <span className="min-w-0 truncate">
          {limitLabel(item)} {item.provider === 'deepseek' ? '' : item.detail || ''}
        </span>
        {item.trailing_text ? <span className="ml-auto shrink-0 pl-2">{item.trailing_text}</span> : null}
      </div>
    </div>
  )
}

export function AiLimitsPanel({ requestGateway }: AiLimitsPanelProps) {
  const { t } = useI18n()
  const [usage, setUsage] = useState<RemoteUsage | null>(null)
  const [limits, setLimits] = useState<LimitItem[]>([])
  const [stale, setStale] = useState(false)
  const [tokenRate, setTokenRate] = useState(0)

  useEffect(() => {
    let disposed = false
    let controller: AbortController | null = null
    let tokenSamples: TokenSample[] = []

    const refresh = async () => {
      controller?.abort()
      controller = new AbortController()

      const remotePromise = requestGateway<ProviderRuntime>('config.get', {
        key: 'provider_runtime',
        provider: REMOTE_PROVIDER
      }).then(async runtime => {
        const baseUrl = String(runtime.base_url || '').replace(/\/v1\/?$/, '')
        const apiKey = String(runtime.api_key || '')

        if (!baseUrl || !apiKey) {
          throw new Error('provider credentials unavailable')
        }

        const headers = { Authorization: `Bearer ${apiKey}` }
        const [limitsResponse, usageResponse] = await Promise.all([
          fetch(`${baseUrl}/v0/user/ai-limits`, {
            cache: 'no-store',
            headers,
            signal: controller?.signal
          }),
          fetch(`${baseUrl}/v0/user/usage`, {
            cache: 'no-store',
            headers,
            signal: controller?.signal
          })
        ])

        if (!limitsResponse.ok) {
          throw new Error(`AI limits endpoint returned ${limitsResponse.status}`)
        }
        if (!usageResponse.ok) {
          throw new Error(`usage endpoint returned ${usageResponse.status}`)
        }

        return {
          limits: mapRemoteLimits((await limitsResponse.json()) as RemoteLimitsPayload),
          usage: (await usageResponse.json()) as RemoteUsage
        }
      })

      const [remoteResult] = await Promise.allSettled([remotePromise])

      if (disposed) {return}

      if (remoteResult.status === 'fulfilled') {
        const nextUsage = remoteResult.value.usage
        const now = Date.now()
        const nextTotal = chargedTotal(nextUsage.tokens)
        const previousSamples = tokenSamples
        const lastSample = previousSamples.at(-1)

        const retained =
          lastSample && nextTotal < lastSample.total
            ? []
            : previousSamples.filter(sample => sample.timestamp >= now - 10 * 60_000)

        const samples = [...retained, { timestamp: now, total: nextTotal }]
        const firstSample = samples[0]
        const elapsedSeconds = firstSample ? (now - firstSample.timestamp) / 1_000 : 0

        tokenSamples = samples
        setTokenRate(
          firstSample && elapsedSeconds > 0
            ? Math.max(0, (nextTotal - firstSample.total) / elapsedSeconds)
            : 0
        )
        setLimits(remoteResult.value.limits)
        setUsage(nextUsage)
        setStale(false)
      } else {
        setStale(true)
      }
    }

    void refresh()
    const timer = window.setInterval(() => void refresh(), 5_000)

    return () => {
      disposed = true
      controller?.abort()
      window.clearInterval(timer)
    }
  }, [requestGateway])

  const models = useMemo(
    () => Object.entries(usage?.by_model || {}).sort(([left], [right]) => left.localeCompare(right)),
    [usage]
  )

  if (!usage && !limits.length) {
    return null
  }

  const total = chargedTotal(usage?.tokens)
  const tokenLimit = numeric(usage?.token_limit)
  const tokenPercent = tokenLimit > 0 ? Math.min(100, (total / tokenLimit) * 100) : 100

  return (
    <section
      aria-label={t.sidebar.aiLimits}
      className={`w-full shrink-0 overflow-hidden bg-transparent px-0 pb-1 pt-0.5${stale ? ' opacity-60' : ''}`}
    >
      <div className="px-1 text-[10px] font-semibold uppercase tracking-wide text-(--ui-text-tertiary)">
        {t.sidebar.aiLimits}
      </div>

      {limits.map(item => (
        <LimitBar item={item} key={`${item.provider}:${item.key}`} />
      ))}

      {usage ? (
        <>
          <div className="relative mt-1 h-5 w-full overflow-hidden rounded-md bg-(--ui-control-background)">
            <div className="absolute inset-y-0 left-0 bg-[#5865f2]" style={{ width: `${tokenPercent}%` }} />
            <div className="absolute inset-0 flex items-center px-2 text-[10px] font-semibold text-white [text-shadow:0_1px_0_rgba(0,0,0,0.35)]">
              <span className="truncate">
                API tokens {formatTokens(total)}
                {tokenLimit > 0 ? ` / ${formatTokens(tokenLimit)}` : ''}
              </span>
              <span className="ml-auto shrink-0 pl-2">{numeric(usage.requests)} req</span>
            </div>
          </div>
          <div className="mt-1 flex min-w-0 items-center px-1 text-[10px] text-(--ui-text-secondary)">
            <span className="min-w-0 truncate">10 мин: {formatTokenRate(tokenRate)} ток/с</span>
          </div>
          {models.map(([model, modelUsage]) => (
            <div
              className="mt-1 flex min-w-0 items-center px-1 text-[10px] text-(--ui-text-secondary)"
              key={model}
            >
              <span className="min-w-0 truncate">{model}</span>
              <span className="ml-auto shrink-0 pl-2">
                {formatTokens(chargedTotal(modelUsage.tokens))} ·{' '}
                {formatTokens(chargedInput(modelUsage.tokens))}/
                {formatTokens(chargedOutput(modelUsage.tokens))}
              </span>
            </div>
          ))}
        </>
      ) : null}
    </section>
  )
}
