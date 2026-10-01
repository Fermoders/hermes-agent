import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { TokenTotalsLine } from './token-stats'

describe('display-only token accounting', () => {
  it('keeps cache writes separate from generated output and unknown distinct from zero', () => {
    render(<TokenTotalsLine label="Task" totals={{ input: 30, output: 0, cache_read: 50, cache_write: null, total: 80, calls: 1, missing_calls: 0, delegated_calls: 1, generation_seconds: 2, tokens_per_second: 0 }} />)
    expect(screen.getByText(/Read 30/)).toBeTruthy()
    expect(screen.getByText(/Cache write —/)).toBeTruthy()
    expect(screen.getByText(/Output 0/)).toBeTruthy()
    expect(screen.getByText(/0.0 tok\/s/)).toBeTruthy()
  })
})
