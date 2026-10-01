import type { GatewayEvent } from '@hermes/shared'
import { act, cleanup } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { createClientSessionState } from '@/lib/chat-runtime'
import { $profileDotStateByScope, profileDotScopeKey } from '@/store/profile-dot-state'
import { $sessions, $unreadFinishedSessionIds } from '@/store/session'
import { $sessionDotStateById, hasLiveTurn, sessionStatusBucket, showsRunningArc } from '@/store/session-dot-state'
import { $sessionStates, clearAllSessionStates, publishSessionState } from '@/store/session-states'
import { $subagentsBySession, reconcileSubagentSnapshot } from '@/store/subagents'
import type { SessionInfo } from '@/types/hermes'

import { renderMessageStream } from './test-harness'

afterEach(() => {
  cleanup()
  clearAllSessionStates()
  $sessions.set([])
  $unreadFinishedSessionIds.set([])
  $subagentsBySession.set({})
})

function mount() {
  const states = new Map([
    ['runtime-a', createClientSessionState('stored-a')],
    ['runtime-b', createClientSessionState('stored-b')]
  ])

  for (const [id, state] of states) {
    publishSessionState(id, state)
  }

  const stream = renderMessageStream('runtime-a', {
    states,
    updateSessionState: (id, updater) => {
      const next = updater(states.get(id) ?? createClientSessionState())
      states.set(id, next)
      publishSessionState(id, next)

      return next
    }
  })

  const emit = (sid: string, type: GatewayEvent['type'], payload: GatewayEvent['payload'] = {}) =>
    act(() => stream.handleEvent({ type, session_id: sid, payload }))

  return { stream, emit }
}

function expectDelegating(id = 'stored-a') {
  const dot = $sessionDotStateById.get()[id]
  expect(dot).toBe('delegating')
  expect(showsRunningArc(dot)).toBe(true)
  expect(sessionStatusBucket(dot)).toBe('working')
  expect(hasLiveTurn(dot)).toBe(false)
}

describe('delegated work in the sidebar', () => {
  it('hydrates cold rows without a composer and clears stale roster activity with explicit zero', () => {
    $sessions.set([
      { id: 'stored-a', profile: 'research', active_descendant_count: 2 },
      { id: 'stored-b', profile: 'default', active_descendant_count: 0 }
    ] as SessionInfo[])
    expectDelegating()
    expect($profileDotStateByScope.get()[profileDotScopeKey(null, 'research')]?.state).toBe('working')
    expect($profileDotStateByScope.get()[profileDotScopeKey(null, 'default')]?.workingCount ?? 0).toBe(0)
    reconcileSubagentSnapshot('stored-a', [{ subagent_id: 'stale', status: 'running' }])
    $sessions.set([{ id: 'stored-a', active_descendant_count: 0 }] as SessionInfo[])
    expect($sessionDotStateById.get()['stored-a'] ?? 'idle').not.toBe('delegating')
  })

  it('routes real session.info events only to the owning profile and connection with colliding ids', () => {
    const { stream } = mount()
    $sessions.set([
      { id: 'stored-b', profile: 'one', connection_id: 'a', active_descendant_count: 0 },
      { id: 'stored-b', profile: 'two', connection_id: 'a', active_descendant_count: 0 },
      { id: 'stored-b', profile: 'one', connection_id: 'b', active_descendant_count: 0 }
    ] as SessionInfo[])
    act(() =>
      stream.handleEvent({
        type: 'session.info',
        session_id: 'runtime-b',
        profile: 'one',
        connectionId: 'b',
        payload: { stored_session_id: 'stored-b', active_descendant_count: 1, running: false }
      })
    )
    expect($sessions.get().map(row => row.active_descendant_count)).toEqual([0, 0, 1])
    expect($profileDotStateByScope.get()[profileDotScopeKey('b', 'one')]?.state).toBe('working')
    expect($profileDotStateByScope.get()[profileDotScopeKey('a', 'one')]?.workingCount ?? 0).toBe(0)
    expect($profileDotStateByScope.get()[profileDotScopeKey('a', 'two')]?.workingCount ?? 0).toBe(0)
  })

  it('applies authoritative session.info counts to background rows and explicit zero', () => {
    const { emit } = mount()
    $sessions.set([{ id: 'stored-b', profile: 'default' }] as SessionInfo[])
    emit('runtime-b', 'session.info', { stored_session_id: 'stored-b', active_descendant_count: 1, running: false })
    expectDelegating('stored-b')
    emit('runtime-b', 'session.info', { stored_session_id: 'stored-b', active_descendant_count: 0, running: false })
    expect($sessionDotStateById.get()['stored-b'] ?? 'idle').not.toBe('delegating')
  })
  it('keeps child work running through parent idle and all child terminal outcomes without completing another turn', () => {
    const { stream, emit } = mount()
    emit('runtime-a', 'message.start')
    emit('runtime-a', 'subagent.start', { subagent_id: 'child-a', status: 'running' })
    emit('runtime-a', 'subagent.start', { subagent_id: 'child-b', parent_id: 'child-a', status: 'running' })
    emit('runtime-a', 'session.info', { running: false })
    expectDelegating()
    expect(stream.state('runtime-a').busy).toBe(false)
    expect($sessionDotStateById.get()['stored-b'] ?? 'idle').not.toBe('delegating')

    emit('runtime-a', 'subagent.complete', { subagent_id: 'child-a', status: 'completed' })
    expectDelegating()
    emit('runtime-b', 'message.start')
    emit('runtime-a', 'subagent.complete', { subagent_id: 'child-b', status: 'failed' })
    expect($sessionDotStateById.get()['stored-a'] ?? 'idle').not.toBe('delegating')
    expect($sessionDotStateById.get()['stored-b']).toBe('working')

    for (const status of ['cancelled', 'interrupted', 'timeout']) {
      emit('runtime-a', 'subagent.start', { subagent_id: status, status: 'running' })
      expectDelegating()
      emit('runtime-a', 'subagent.complete', { subagent_id: status, status })
      expect($sessionDotStateById.get()['stored-a'] ?? 'idle').not.toBe('delegating')
      expect(stream.state('runtime-b').busy).toBe(true)
    }

    emit('runtime-b', 'subagent.start', { subagent_id: 'own-child', status: 'running' })
    emit('runtime-b', 'subagent.complete', { subagent_id: 'own-child', status: 'completed' })
    expect($sessionDotStateById.get()['stored-b']).toBe('working')
    expect(stream.state('runtime-b').busy).toBe(true)
  })

  it('reconstructs descendant activity from a reconnect roster, across lineage aliases, not transient events', () => {
    mount()
    $sessions.set([
      { id: 'tip-a', profile: 'research', _lineage_root_id: 'stored-a', _lineage_ids: ['stored-a'] },
      { id: 'stored-b', profile: 'default' }
    ] as SessionInfo[])
    reconcileSubagentSnapshot('runtime-a', [
      { subagent_id: 'ancestor', status: 'completed' },
      { subagent_id: 'descendant', parent_id: 'ancestor', status: 'running' }
    ])
    expectDelegating('tip-a')
    expectDelegating('stored-a')
    expect($profileDotStateByScope.get()[profileDotScopeKey(null, 'research')]?.state).toBe('working')
    expect($profileDotStateByScope.get()[profileDotScopeKey(null, 'default')]?.workingCount ?? 0).toBe(0)
    expect($sessionStates.get()['runtime-a']?.busy).toBe(false)
    expect($sessionDotStateById.get()['stored-b'] ?? 'idle').not.toBe('delegating')
    reconcileSubagentSnapshot('runtime-a', [])
    expect($sessionDotStateById.get()['tip-a'] ?? 'idle').not.toBe('delegating')
  })
})
