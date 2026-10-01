import { afterEach, expect, it } from 'vitest'
import { $cronSessions, $messagingSessions, $sessions, reconcileSessionDescendantActivity } from './session'
import { $delegatingSessionIds, $sessionDotStateById, sessionDotStateFor, sessionStatusBucket } from './session-dot-state'
import { $profileDotStateByScope } from './profile-dot-state'
import { setSidebarOrdering } from './layout'
import { $sidebarSessionRankIds } from './sidebar-sort'
import { clearAllSessionStates, publishSessionState, recordSessionEventScope } from './session-states'
import { $subagentsBySession, reconcileSubagentSnapshot } from './subagents'
import { createClientSessionState } from '@/lib/chat-runtime'
import type { SessionInfo } from '@/types/hermes'
import { backendScopeKey } from '@hermes/shared'

const key = (connection: string, profile: string, id = 'same') => `${backendScopeKey(connection, profile)}::${id}`
const row = (connection_id: string, profile: string, count: number): SessionInfo =>
  ({ id: 'same', connection_id, profile, active_descendant_count: count }) as SessionInfo

afterEach(() => {
  $sessions.set([])
  $messagingSessions.set([])
  $cronSessions.set([])
  $subagentsBySession.set({})
  clearAllSessionStates()
  setSidebarOrdering('updated')
})

it.each([
  ['a', 'one', 'a', 'two'],
  ['a', 'one', 'b', 'one'],
  ['a', 'one', 'b', 'two']
])('isolates distinct compression tips sharing a root: %s/%s vs %s/%s', (a, one, b, two) => {
  const active = { ...row(a, one, 1), id: 'tip-a', _lineage_root_id: 'root', _lineage_ids: ['root', 'tip-a'] }
  const idle = { ...row(b, two, 0), id: 'tip-b', _lineage_root_id: 'root', _lineage_ids: ['root', 'tip-b'] }
  for (const rows of [[idle, active], [active, idle]]) {
    $sessions.set(rows)
    const dots = $sessionDotStateById.get()
    expect(sessionDotStateFor(dots, idle)).toBe('idle')
    expect(dots[key(b, two, 'tip-b')]).not.toBe('delegating')
    expect(sessionDotStateFor(dots, active)).toBe('delegating')
    expect(dots[key(a, one, 'root')]).toBe('delegating')
    expect(dots[key(b, two, 'root')]).not.toBe('delegating')
    expect(dots.root).toBeUndefined()
    expect($delegatingSessionIds.get()).not.toContain('root')
    expect(rows.filter(r => sessionStatusBucket(sessionDotStateFor(dots, r)) === 'working')).toEqual([active])
    expect($profileDotStateByScope.get()[backendScopeKey(b, two)]?.workingCount ?? 0).toBe(0)
    expect($profileDotStateByScope.get()[backendScopeKey(a, one)]?.workingCount).toBe(1)
    setSidebarOrdering('status')
    expect($sidebarSessionRankIds.get()).toEqual(['tip-a', 'tip-b'])
  }

  recordSessionEventScope({ session_id: 'runtime-root', connectionId: b, profile: two })
  publishSessionState('runtime-root', { ...createClientSessionState('root'), busy: true })
  expect(sessionDotStateFor($sessionDotStateById.get(), idle)).toBe('working')
  expect(sessionDotStateFor($sessionDotStateById.get(), active)).toBe('delegating')
})

it('newer rosters supersede cold counts in both directions, and a later list supersedes the roster', () => {
  $sessions.set([{ id: 'same', active_descendant_count: 0 }] as SessionInfo[])
  reconcileSubagentSnapshot('same', [{ subagent_id: 'child', status: 'running' }])
  expect($sessionDotStateById.get().same).toBe('delegating')
  $sessions.set([{ id: 'same', active_descendant_count: 1 }] as SessionInfo[])
  reconcileSubagentSnapshot('same', [])
  expect($sessionDotStateById.get().same ?? 'idle').toBe('idle')
  $sessions.set([{ id: 'same', active_descendant_count: 1 }] as SessionInfo[])
  expect($sessionDotStateById.get().same).toBe('delegating')
})

it('session.info reconciliation and lineage activity stay within the exact connection and profile', () => {
  const rows = [row('a', 'one', 0), row('a', 'two', 0), row('b', 'one', 0)].map(r => ({
    ...r,
    _lineage_root_id: 'root',
    _lineage_ids: ['root', 'same']
  }))
  $sessions.set(rows)
  reconcileSessionDescendantActivity('same', 1, { connectionId: 'a', profile: 'one' })
  expect($sessions.get().map(r => r.active_descendant_count)).toEqual([1, 0, 0])
  expect($sessionDotStateById.get()[key('a', 'one')]).toBe('delegating')
  expect($sessionDotStateById.get()[key('a', 'two')] ?? 'idle').toBe('idle')
  expect($sessionDotStateById.get()[key('b', 'one')] ?? 'idle').toBe('idle')
  recordSessionEventScope({ session_id: 'runtime', connectionId: 'b', profile: 'one' })
  publishSessionState('runtime', createClientSessionState('same'))
  reconcileSubagentSnapshot('runtime', [{ subagent_id: 'child', status: 'running' }])
  expect($sessionDotStateById.get()[key('b', 'one')]).toBe('delegating')
  expect($sessionDotStateById.get()[key('a', 'two')] ?? 'idle').toBe('idle')
})

it('cold messaging and cron activity subscribe to their actual stores without a recent row', () => {
  $messagingSessions.set([row('a', 'one', 1)])
  $cronSessions.set([row('b', 'two', 1)])
  expect($sessionDotStateById.get()[key('a', 'one')]).toBe('delegating')
  expect($sessionDotStateById.get()[key('b', 'two')]).toBe('delegating')
  reconcileSessionDescendantActivity('same', 0, { connectionId: 'b', profile: 'two' })
  expect($cronSessions.get()[0]?.active_descendant_count).toBe(0)
  expect($sessionDotStateById.get()[key('b', 'two')] ?? 'idle').toBe('idle')
  expect($sessionDotStateById.get()[key('a', 'one')]).toBe('delegating')
})
