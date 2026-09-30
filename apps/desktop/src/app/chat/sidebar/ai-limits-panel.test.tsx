import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'

import { AiLimitsPanel } from './ai-limits-panel'

vi.mock('@/i18n', () => ({ useI18n: () => ({ t: { sidebar: { aiLimits: 'AI limits' } } }) }))
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks() })

function payload(total = 1000) {
  return {
    limits: { models: {
      gpt: { windows: [{ key: 'gpt', label: '7д', detail: '79%/74%', used_percent: 79, elapsed_percent: 74 }] },
      gpt_codex_spark: { windows: [{ key: 'spark', label: 'Spark 7д', detail: '13%/8%', used_percent: 13 }] }
    } },
    usage: { requests: 50, token_limit: 1000000, tokens: { charged_total_tokens: total }, by_model: {
      'deepseek-v4-flash': { tokens: { charged_total_tokens: 500, charged_input_tokens: 400, charged_output_tokens: 100 } }
    } }
  }
}

test('requests capability-discovered limits via gateway without renderer credentials', async () => {
  const requestGateway = vi.fn().mockResolvedValue(payload())
  await act(async () => { render(<AiLimitsPanel requestGateway={requestGateway} />) })
  expect(requestGateway).toHaveBeenCalledWith('ai_limits.get')
  expect(screen.queryByText(/GPT 7д 79%\/74%/)).not.toBeNull()
  expect(screen.queryByText(/Spark 7д 13%\/8%/)).not.toBeNull()
  expect(screen.queryByText('deepseek-v4-flash')).not.toBeNull()
  expect(screen.queryByText(/API tokens/)?.closest('.relative')?.className).toContain('rounded-md')
})

test('hides unavailable limits rather than using local dashboard usage', async () => {
  const requestGateway = vi.fn().mockRejectedValue(new Error('unavailable'))
  await act(async () => { render(<AiLimitsPanel requestGateway={requestGateway} />) })
  expect(screen.queryByText('AI limits')).toBeNull()
})

test('shows charged rolling throughput and preserves stale data on failures', async () => {
  vi.useFakeTimers()
  const requestGateway = vi.fn().mockResolvedValue(payload())
  await act(async () => { render(<AiLimitsPanel requestGateway={requestGateway} />) })
  requestGateway.mockResolvedValue(payload(1500))
  await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
  expect(screen.queryByText('10 мин: 100 ток/с')).not.toBeNull()
  requestGateway.mockRejectedValue(new Error('offline'))
  await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
  expect(screen.getByLabelText('AI limits').className).toContain('opacity-60')
})
