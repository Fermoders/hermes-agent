import { act, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, test, vi } from 'vitest'

import { AiLimitsPanel } from './ai-limits-panel'

vi.mock('@/i18n', () => ({ useI18n: () => ({ t: { sidebar: { aiLimits: 'AI limits' } } }) }))

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('AiLimitsPanel', () => {
  test('renders GPT, DeepSeek, total token usage, and model rows', async () => {
    const requestGateway = vi.fn().mockResolvedValue({
      base_url: 'https://82-24-174-162.sslip.io/v1',
      limits: {
        models: {
          gpt: {
            windows: [{ key: 'secondary_window', label: '7д', detail: '79%/74%', used_percent: 79, elapsed_percent: 74, remaining_text: '44ч' }]
          },
          gpt_codex_spark: {
            windows: [{ key: 'spark', label: 'GPT-5.3-Codex-Spark ChatGPT 7д', detail: '13%/8%', used_percent: 13, elapsed_percent: 8, remaining_text: '154ч' }]
          }
        }
      },
      usage: {
        requests: 50,
        token_limit: 500000000,
        tokens: { charged_total_tokens: 23638099 },
        by_model: {
          'gpt-5.6-sol': {
            tokens: {
              charged_total_tokens: 20000000,
              charged_input_tokens: 15000000,
              charged_output_tokens: 5000000
            }
          },
          'deepseek-v4-flash': {
            tokens: {
              charged_total_tokens: 3638099,
              charged_input_tokens: 3000000,
              charged_output_tokens: 638099
            }
          }
        }
      }
    })

    await act(async () => {
      render(<AiLimitsPanel requestGateway={requestGateway} />)
      await Promise.resolve()
    })

    await vi.waitFor(() => expect(screen.queryByText('AI limits')).not.toBeNull())
    expect(screen.queryByText(/GPT 7д 79%\/74%/)).not.toBeNull()
    expect(screen.queryByText(/GPT-5.3-Codex-Spark ChatGPT 7д 13%\/8%/)).not.toBeNull()
    const tokenUsage = screen.queryByText(/API tokens/)
    expect(tokenUsage).not.toBeNull()
    expect(tokenUsage?.closest('.relative')?.className).toContain('rounded-md')
    expect(tokenUsage?.closest('.relative')?.className).not.toContain('rounded-full')
    expect(screen.queryByText('gpt-5.6-sol')).not.toBeNull()
    expect(screen.queryByText('deepseek-v4-flash')).not.toBeNull()
    expect(requestGateway).toHaveBeenCalledWith('ai_limits.get')
  })

  test('loads limits through the gateway without selecting a provider by name', async () => {
    const requestGateway = vi.fn().mockResolvedValue({
      base_url: 'https://proxy.example/v1',
      limits: {
        models: {
          gpt: {
            windows: [{ key: 'weekly', label: '7д', detail: '50%/50%', used_percent: 50 }]
          }
        }
      },
      usage: {
        requests: 1,
        token_limit: 10,
        tokens: { charged_total_tokens: 5 },
        by_model: {}
      }
    })
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    await act(async () => {
      render(<AiLimitsPanel requestGateway={requestGateway} />)
      await Promise.resolve()
    })

    await vi.waitFor(() => expect(screen.queryByText(/GPT 7д 50%\/50%/)).not.toBeNull())
    expect(requestGateway).toHaveBeenCalledWith('ai_limits.get')
    expect(requestGateway).not.toHaveBeenCalledWith(
      'config.get',
      expect.objectContaining({ provider: expect.anything() })
    )
    expect(fetchMock).not.toHaveBeenCalled()
  })

  test('does not fall back to the local dashboard when the remote provider is unavailable', async () => {
    const requestGateway = vi.fn().mockRejectedValue(new Error('missing key'))
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    await act(async () => {
      render(<AiLimitsPanel requestGateway={requestGateway} />)
      await Promise.resolve()
    })

    await vi.waitFor(() => expect(requestGateway).toHaveBeenCalled())
    expect(fetchMock).not.toHaveBeenCalled()
    expect(screen.queryByText('AI limits')).toBeNull()
  })

  test('shows charged token throughput over the rolling last ten minutes', async () => {
    vi.useFakeTimers()

    let remoteTotal = 1_000
    const requestGateway = vi.fn().mockImplementation(async () => ({
      base_url: 'https://82-24-174-162.sslip.io/v1',
      limits: {
        models: {
          gpt: { windows: [{ key: 'gpt', label: '7д', detail: '1%/1%', used_percent: 1 }] },
          gpt_codex_spark: { windows: [{ key: 'spark', label: 'Spark 7д', detail: '2%/1%', used_percent: 2 }] }
        }
      },
      usage: {
        requests: 1,
        token_limit: 1_000_000,
        tokens: { charged_total_tokens: remoteTotal },
        by_model: {}
      }
    }))

    await act(async () => {
      render(<AiLimitsPanel requestGateway={requestGateway} />)
      await Promise.resolve()
    })
    await vi.waitFor(() => expect(screen.queryByText(/API tokens/)).not.toBeNull())

    remoteTotal = 1_500
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000)
    })

    expect(screen.queryByText('10 мин: 100 ток/с')).not.toBeNull()
    vi.useRealTimers()
  })
})
