import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { atom } from 'nanostores'
import { createClientSessionState, toRuntimeMessage } from '@/lib/chat-runtime'
import { toChatMessages } from '@/lib/chat-messages'
import { renderMessageStream } from '@/app/session/hooks/use-message-stream/test-harness'
import { $gateway } from '@/store/gateway'
import { $sessionStates, clearAllSessionStates, publishSessionState } from '@/store/session-states'
import { ChatTokenPanel, ResponseTokenFooter } from './token-stats'
import { useSessionActions } from '@/app/session/hooks/use-session-actions'
import { setSessions, setActiveSessionId, setSelectedStoredSessionId } from '@/store/session'
import { useRef } from 'react'

vi.mock('@/hermes', async importOriginal => ({ ...await importOriginal<object>(), getLatestSessionMessages: vi.fn(async () => ({ session_id: 'stored', messages: [{ role: 'assistant', content: 'final', row_id: 30 }] })) }))
vi.mock('@/store/session-request-router', async importOriginal => ({ ...await importOriginal<object>(), requestForSessionProfile: (_owner: unknown, request: any, method: string, params: unknown) => request(method, params) }))

const fixtures = vi.hoisted(() => ({ message: {} as any, request: vi.fn() }))
vi.mock('@assistant-ui/react', async importOriginal => ({ ...await importOriginal<object>(), useAuiState: (select: any) => select({ message: fixtures.message }) }))
const runtimeId = atom<string | null>('tokens-regression')
const busy = atom(false)
vi.mock('./session-view', async importOriginal => ({ ...await importOriginal<object>(), useSessionView: () => ({ $runtimeId: runtimeId, $busy: busy }) }))
vi.mock('@/store/session-states', async importOriginal => ({ ...await importOriginal<object>(), requestForOwnedSession: (...args: any[]) => fixtures.request(...args) }))

const totals = (output = 10) => ({ input: 30, output, cache_read: 50, cache_write: null, total: 80 + output, calls: 1, missing_calls: 0, delegated_calls: 1, generation_seconds: 2, tokens_per_second: output / 2 })
const stats = (output = 10, task = 'task') => ({ task_id: task, task: totals(output), session: totals(output), response: totals(output), responses: { '30': totals(output) } })
function publish(output = 10) {
  publishSessionState('tokens-regression', { ...createClientSessionState('stored'), usage: { token_stats: stats(output) } as any })
}
function deferred() {
  let resolve!: (value: any) => void
  const promise = new Promise<any>(r => { resolve = r })
  return { promise, resolve }
}
afterEach(() => { cleanup(); clearAllSessionStates(); setSessions([]); setActiveSessionId(null); setSelectedStoredSessionId(null); $gateway.set(null); fixtures.request.mockReset(); vi.useRealTimers() })

describe('token telemetry regressions', () => {
  it('hydrates cold resume and warm activate usage through the real session actions hook', async () => {
    setSessions([{ id: 'stored', message_count: 1, input_tokens: 30, output_tokens: 10, started_at: 1, last_active: 1, source: 'desktop' }] as any)
    const request = vi.fn(async (method: string) => {
      expect(['session.resume', 'session.activate']).toContain(method)
      return { session_id: 'tokens-regression', session_key: 'stored', resumed: 'stored', messages: [],
        info: { usage: { calls: 1, input: 30, output: method === 'session.resume' ? 10 : 25,
          total: 55, token_stats: stats(method === 'session.resume' ? 10 : 25) } } }
    })
    let actions!: ReturnType<typeof useSessionActions>
    function Harness() {
      const activeSessionIdRef = useRef<string | null>(null)
      const selectedStoredSessionIdRef = useRef<string | null>(null)
      const states = useRef(new Map<string, ReturnType<typeof createClientSessionState>>())
      const ids = useRef(new Map<string, string>())
      const update = (id: string, updater: any, storedId?: string | null) => {
        const next = updater(states.current.get(id) ?? createClientSessionState(storedId ?? 'stored'))
        states.current.set(id, next)
        ids.current.set(next.storedSessionId, id)
        publishSessionState(id, next)
        return next
      }
      actions = useSessionActions({ activeSessionId: null, activeSessionIdRef, selectedStoredSessionId: null,
        selectedStoredSessionIdRef, busyRef: useRef(false), creatingSessionRef: useRef(false),
        sessionStateByRuntimeIdRef: states, runtimeIdByStoredSessionIdRef: ids,
        ensureSessionState: id => states.current.get(id) ?? createClientSessionState('stored'),
        updateSessionState: update, syncSessionStateToView: (id, state) => publishSessionState(id, state),
        getRouteToken: () => 'token', getRoutedStoredSessionId: () => null, resetViewSync: vi.fn(),
        navigate: vi.fn(), requestGateway: request as any })
      return null
    }
    fixtures.message = toRuntimeMessage({ id: 'final', role: 'assistant', rowId: 30, parts: [{ type: 'text', text: 'final', sourceRowId: 30 }] })
    render(<><Harness /><ChatTokenPanel /><ResponseTokenFooter /></>)
    await act(async () => { await actions.resumeSession('stored') })
    expect(request).toHaveBeenCalledWith('session.resume', expect.objectContaining({ session_id: 'stored' }))
    expect(screen.getAllByText(/Output 10/)).toHaveLength(3)
    await act(async () => { await actions.resumeSession('stored') })
    expect(request).toHaveBeenCalledWith('session.activate', expect.objectContaining({ session_id: 'tokens-regression' }))
    expect(screen.getAllByText(/Output 25/)).toHaveLength(3)
  })
  it.each(['receipt', 'hydrated'])('renders ResponseTokenFooter from the final folded row (%s)', mode => {
    publish()
    const folded = { id: 'folded', role: 'assistant', rowId: 20,
      parts: [{ type: 'text', text: 'first', sourceRowId: 20 }, { type: 'text', text: 'final', sourceRowId: 30 }],
      ...(mode === 'receipt' ? { persistedTurn: { final_assistant_row_id: 30, complete: true } } : {}) }
    const hydrated = toChatMessages([
      { role: 'user', content: 'question', row_id: 10 },
      { role: 'assistant', content: 'first', row_id: 20 },
      { role: 'assistant', content: '', row_id: 21, tool_calls: [{ id: 'tool', function: { name: 'read_file', arguments: '{}' } }] },
      { role: 'tool', content: 'result', tool_call_id: 'tool', row_id: 22 },
      { role: 'assistant', content: 'final', row_id: 30 }
    ] as any).find(message => message.role === 'assistant')!
    fixtures.message = toRuntimeMessage(mode === 'receipt' ? folded as any : hydrated)
    render(<ResponseTokenFooter />)
    expect(screen.getByText(/Output 10/)).toBeTruthy()
  })
  it('publishes completed usage between polls', async () => {
    const initial = createClientSessionState('stored', [{ id: 'folded', role: 'assistant', rowId: 20,
      pending: true, parts: [{ type: 'text', text: 'first', sourceRowId: 20 }] }])
    initial.streamId = 'folded'
    initial.busy = true
    const stream = renderMessageStream('tokens-regression', { states: new Map([['tokens-regression', initial]]) })
    act(() => stream.handleEvent({ session_id: 'tokens-regression', type: 'tool.start', payload: { name: 'read_file', tool_id: 'tool', args: {} } }))
    act(() => stream.handleEvent({ session_id: 'tokens-regression', type: 'tool.complete', payload: { name: 'read_file', tool_id: 'tool', result: 'result' } }))
    act(() => stream.handleEvent({ session_id: 'tokens-regression', type: 'message.complete', payload: { text: 'final', persisted_turn: { final_assistant_row_id: 30, complete: true } } }))
    fixtures.message = toRuntimeMessage(stream.state().messages.at(-1)!)
    const old = deferred()
    fixtures.request.mockReturnValue(old.promise)
    $gateway.set({ request: vi.fn() } as any)
    publish()
    render(<ChatTokenPanel />)
    await act(async () => { old.resolve({ token_stats: stats(10) }) })
    render(<ResponseTokenFooter />)
    expect(screen.getAllByText(/Output 10/)).toHaveLength(3)
    const usageStream = renderMessageStream('tokens-regression', {
      updateSessionState: (id, updater) => {
        const next = updater($sessionStates.get()[id] ?? createClientSessionState('stored'))
        publishSessionState(id, next)
        return next
      }
    })
    act(() => usageStream.handleEvent({ session_id: 'tokens-regression', type: 'message.complete', payload: {
      text: 'final', usage: { calls: 1, input: 30, output: 25, total: 55, token_stats: stats(25) }
    } }))
    expect(screen.getAllByText(/Output 25/)).toHaveLength(3)
  })
  it('rejects a read overtaken by a completion event', async () => {
    const late = deferred()
    fixtures.request.mockReturnValueOnce(late.promise)
    $gateway.set({ request: vi.fn() } as any)
    publish()
    render(<ChatTokenPanel />)
    act(() => publish(25))
    await act(async () => { late.resolve({ token_stats: stats(10) }) })
    expect(screen.getAllByText(/Output 25/)).toHaveLength(2)
  })
  it('rejects overlapping reads out of order', async () => {
    vi.useFakeTimers()
    const first = deferred(), second = deferred()
    fixtures.request.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
    $gateway.set({ request: vi.fn() } as any)
    publish()
    render(<ChatTokenPanel />)
    await act(async () => { vi.advanceTimersByTime(3000) })
    await act(async () => { second.resolve({ token_stats: stats(25) }) })
    await act(async () => { first.resolve({ token_stats: stats(10) }) })
    expect(screen.getAllByText(/Output 25/)).toHaveLength(2)
  })
  it.each(['session.usage', 'message.complete'])('preserves newer %s usage over a delayed reset snapshot', async eventType => {
    const resetReply = deferred()
    const resetSnapshot = { token_stats: stats(0, 'new-task') }
    let backendReset = false
    fixtures.request.mockResolvedValueOnce({ token_stats: stats(10) }).mockImplementationOnce((_sid, _request, method) => {
      expect(method).toBe('session.tokens.new_task')
      backendReset = true // The backend mutation precedes its delayed reply.
      return resetReply.promise
    })
    $gateway.set({ request: vi.fn() } as any)
    publish()
    const stream = renderMessageStream('tokens-regression', {
      updateSessionState: (id, updater) => {
        const next = updater($sessionStates.get()[id] ?? createClientSessionState('stored'))
        publishSessionState(id, next)
        return next
      }
    })
    fixtures.message = toRuntimeMessage({ id: 'final', role: 'assistant', rowId: 30, parts: [{ type: 'text', text: 'final', sourceRowId: 30 }] })
    render(<><ChatTokenPanel /><ResponseTokenFooter /></>)
    await act(async () => {})
    act(() => { fireEvent.click(screen.getByRole('button')) })
    expect(backendReset).toBe(true)
    act(() => stream.handleEvent({ session_id: 'tokens-regression', type: eventType, payload: {
      text: 'final', persisted_turn: { final_assistant_row_id: 30, complete: true },
      usage: { calls: 1, input: 30, output: 25, total: 55, token_stats: stats(25, 'new-task') }
    } } as any))
    expect(screen.getAllByText(/Output 25/)).toHaveLength(3)
    const newerUsage = $sessionStates.get()['tokens-regression']?.usage
    await act(async () => { resetReply.resolve(resetSnapshot) })
    expect($sessionStates.get()['tokens-regression']?.usage).toBe(newerUsage)
    expect($sessionStates.get()['tokens-regression']?.usage?.token_stats?.task_id).toBe('new-task')
    expect(screen.getAllByText(/Output 25/)).toHaveLength(3)
  })
  it('rejects a late pre-reset read', async () => {
    const late = deferred()
    fixtures.request.mockReturnValueOnce(late.promise).mockResolvedValueOnce({ token_stats: stats(0, 'new-task') })
    $gateway.set({ request: vi.fn() } as any)
    publish()
    render(<ChatTokenPanel />)
    await act(async () => { fireEvent.click(screen.getByRole('button')) })
    await act(async () => { late.resolve({ token_stats: stats(25) }) })
    expect(screen.getAllByText(/Output 0/)).toHaveLength(2)
    expect($sessionStates.get()['tokens-regression']?.usage?.token_stats?.task_id).toBe('new-task')
  })
})
