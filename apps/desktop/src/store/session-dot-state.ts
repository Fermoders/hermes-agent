/**
 * SESSION DOT STATE — one map from session id to the single status a surface
 * should paint, so the sidebar, the pane tabs, and the switcher can never
 * disagree about what a session is doing.
 *
 * It exists for two reasons the individual membership atoms cannot cover:
 *
 * 1. PRIORITY IN ONE PLACE. The signals overlap — a session can be working AND
 *    unread AND running a background job — and resolving that per call site is
 *    how surfaces drift apart.
 * 2. LINEAGE. Compression rotates a conversation's stored id, so a sidebar row,
 *    a persisted tile, and the route can each hold a different tip of one
 *    lineage. Every state is claimed under every alias (see `lineageAliases`),
 *    so a surface gets the right answer whichever tip it happens to hold.
 *
 * The inputs are all reference-stable across stream deltas, so this recomputes
 * on status edges rather than per token.
 *
 * Unread has TWO sources, both claiming the same state: the runtime marker
 * (a turn finished in the background while this window wasn't looking at it,
 * $unreadFinishedSessionIds — transient) and the backend's derived read-state
 * watermark (row.unread — persists across restarts and is visible to every
 * surface). The write side of the persisted flag lives in session-unread.ts.
 */

import { computed } from 'nanostores'

import { stableArray, stableRecord } from '@/lib/stable-array'

import { $backgroundRunningSessionIds } from './composer-status'
import { backendScopeKey } from '@hermes/shared'
import {
  $cronSessions,
  $messagingSessions,
  $sessions,
  $unreadFinishedSessionIds,
  lineageAliases,
  sessionActivityKey,
  sessionMatchesStoredId
} from './session'
import { $sessionActivityObservationEpoch, sessionActivityObservation } from './session-activity-observation'
import { runtimeSessionOwner } from './session-states'
import type { SessionInfo } from '@/types/hermes'
import {
  $attentionSessionIds,
  $draftSessionIds,
  $sessionStates,
  $stalledSessionIds,
  $workingSessionIds
} from './session-states'
import { $unreadWriteGuard, UNREAD_WRITE_GUARD_MS } from './session-unread-remote'
import { $subagentsBySession, activeSubagentCount } from './subagents'

// Sessions parked in async delegation: the parent turn has ended (busy=false —
// delegate_task(background=true) returns its handle the moment the children
// are spawned) while those subagents keep working for minutes. Without this
// input the sidebar row dropped to a plain idle dot mid-delegation, reading as
// "done" while work was still running in child sessions. Same runtime→stored
// bridge and fresh-chat fallback as $backgroundRunningSessionIds:
// $subagentsBySession is keyed by runtime id, surfaces key on stored ids, and
// lineageAliases covers whichever tip of the conversation a surface holds.
let delegatingIds: readonly string[] = []
export const $delegatingSessionIds = computed(
  [$subagentsBySession, $sessionStates, $sessions, $messagingSessions, $cronSessions, $sessionActivityObservationEpoch],
  (bySession, states, sessions, messaging, cron) => {
    const rows = [...sessions, ...messaging, ...cron]
    const facts = new Map<string, { count: number; revision: number }>()
    const publish = (key: string, count: number, revision: number) => {
      if (revision >= (facts.get(key)?.revision ?? -1)) facts.set(key, { count, revision })
    }
    for (const row of rows) {
      if (typeof row.active_descendant_count !== 'number') continue
      const family = rows.filter(
        r => backendScopeKey(r.connection_id, r.profile) === backendScopeKey(row.connection_id, row.profile)
      )
      for (const alias of lineageAliases(row.id, family)) {
        publish(sessionActivityKey({ ...row, id: alias }), row.active_descendant_count, sessionActivityObservation(row))
      }
    }
    for (const [runtimeId, items] of Object.entries(bySession)) {
      const stored = states[runtimeId]?.storedSessionId ?? runtimeId
      const owner = runtimeSessionOwner(runtimeId)
      const scope =
        typeof owner === 'string'
          ? backendScopeKey(null, owner)
          : owner
            ? backendScopeKey(owner.connectionId, owner.profile)
            : null
      const matches = rows.filter(
        row =>
          sessionMatchesStoredId(row, stored) && (!scope || backendScopeKey(row.connection_id, row.profile) === scope)
      )
      const scopes = new Set(matches.map(row => backendScopeKey(row.connection_id, row.profile)))
      if (!scope && scopes.size > 1) continue
      const count = activeSubagentCount(items)
      const revision = sessionActivityObservation(items)
      if (!matches.length) publish(stored, count, revision)
      for (const row of matches) {
        const family = rows.filter(
          r => backendScopeKey(r.connection_id, r.profile) === backendScopeKey(row.connection_id, row.profile)
        )
        for (const alias of lineageAliases(stored, family))
          publish(sessionActivityKey({ ...row, id: alias }), count, revision)
      }
    }
    const ids = new Set<string>()
    for (const [key, fact] of facts) if (fact.count > 0) ids.add(key)
    // A unique tip does not make its compression root unique across backends.
    for (const row of rows) {
      const family = rows.filter(
        r => backendScopeKey(r.connection_id, r.profile) === backendScopeKey(row.connection_id, row.profile)
      )
      for (const alias of lineageAliases(row.id, family)) {
        const scopes = new Set(
          rows.filter(r => sessionMatchesStoredId(r, alias)).map(r => backendScopeKey(r.connection_id, r.profile))
        )
        if (scopes.size === 1 && ids.has(sessionActivityKey({ ...row, id: alias }))) ids.add(alias)
      }
    }
    return (delegatingIds = stableArray(delegatingIds, [...ids]))
  }
)

export function sessionDotStateFor(
  states: Readonly<Record<string, SessionDotState>>,
  row: Pick<SessionInfo, 'id' | 'profile' | 'connection_id'>
): SessionDotState {
  return states[sessionActivityKey(row)] ?? states[row.id] ?? 'idle'
}

export type SessionDotState =
  'background' | 'delegating' | 'draft' | 'idle' | 'needs-input' | 'stalled' | 'unread' | 'working'

/** The sidebar row's arc. A quiet turn is still authoritatively running, so
 *  `stalled` keeps it; delegated children keep it without a live parent turn.
 *  A blocking prompt drops it, because the amber dot is the
 *  louder cue and two treatments at once fight each other. */
export const showsRunningArc = (state: SessionDotState): boolean =>
  state === 'delegating' || state === 'stalled' || state === 'working'

/** Whether this turn is the session's own, live: brighter title, and the row's
 *  age yields to the actions menu. Child-only work is not a parent turn; a
 *  turn waiting on an answer has not ended. */
export const hasLiveTurn = (state: SessionDotState): boolean =>
  state === 'working' || state === 'stalled' || state === 'needs-input'

/** The buckets the sidebar's status filter and ordering work in. `stalled` and
 *  `background` fold into the state a user would name them. */
export type SessionStatusBucket = 'draft' | 'idle' | 'needs-input' | 'unread' | 'working'

export const sessionStatusBucket = (state: SessionDotState = 'idle'): SessionStatusBucket =>
  state === 'stalled' || state === 'background' || state === 'delegating' ? 'working' : state

const STATUS_RANK: Record<SessionStatusBucket, number> = {
  'needs-input': 0,
  working: 1,
  unread: 2,
  draft: 3,
  idle: 4
}

/** Loudest first — what ordering by status sorts on. */
export const sessionStatusRank = (state?: SessionDotState): number => STATUS_RANK[sessionStatusBucket(state)]

let dotStates: Readonly<Record<string, SessionDotState>> = {}

export const $sessionDotStateById = computed(
  [
    $attentionSessionIds,
    $workingSessionIds,
    $stalledSessionIds,
    $backgroundRunningSessionIds,
    $delegatingSessionIds,
    $unreadFinishedSessionIds,
    $draftSessionIds,
    $sessions,
    $messagingSessions,
    $cronSessions,
    $sessionStates,
    $unreadWriteGuard
  ],
  (
    attention,
    working,
    stalled,
    background,
    delegating,
    unread,
    draft,
    recent,
    messaging,
    cron,
    states,
    unreadWriteGuard
  ) => {
    const sessions = [...recent, ...messaging, ...cron]
    const next: Record<string, SessionDotState> = {}

    const scopesByAlias = new Map<string, Set<string>>()
    const qualifiedAliases = new Map<string, string[]>()
    for (const row of sessions) {
      const scope = backendScopeKey(row.connection_id, row.profile)
      const family = sessions.filter(r => backendScopeKey(r.connection_id, r.profile) === scope)
      const aliases = lineageAliases(row.id, family)
      const keys = aliases.map(id => sessionActivityKey({ ...row, id }))
      for (const alias of aliases) {
        const scopes = scopesByAlias.get(alias) ?? new Set<string>()
        scopes.add(scope)
        scopesByAlias.set(alias, scopes)
        qualifiedAliases.set(sessionActivityKey({ ...row, id: alias }), keys)
      }
    }
    const claimAliases = (id: string): string[] => {
      const qualified = qualifiedAliases.get(id)
      if (qualified) return qualified
      const scopes = scopesByAlias.get(id)
      if (!scopes) return [id]
      if (scopes.size !== 1) return []
      const scope = [...scopes][0]
      const family = sessions.filter(r => backendScopeKey(r.connection_id, r.profile) === scope)
      return lineageAliases(id, family).flatMap(alias => [
        `${scope}::${alias}`,
        ...(scopesByAlias.get(alias)?.size === 1 ? [alias] : [])
      ])
    }
    const claim = (ids: readonly string[], state: SessionDotState) => {
      for (const id of ids) {
        for (const alias of claimAliases(id)) next[alias] = state
      }
    }

    // Weakest claim first — each pass overwrites the one above it, so the order
    // below IS the priority order. A blocking prompt outranks everything: it is
    // the only state that needs the user.
    //
    // Draft is weakest of all: it says only "no turn has happened here yet", so
    // the first thing that does happen speaks over it.
    claim(draft, 'draft')
    claim(unread, 'unread')

    // Persisted read state (backend watermark): a row marked unread keeps the
    // same emerald dot a background finish would paint, and survives
    // restarts. Same tier as the runtime marker — both mean "there is
    // something here you haven't opened". A list page that predates one of
    // our own writes is fenced out by the write guard: keep OUR value until a
    // page confirms it or the guard expires.
    const persistedUnread: string[] = []

    for (const s of sessions) {
      const entry = unreadWriteGuard.get(s.id)

      if (entry && Date.now() - entry.at < UNREAD_WRITE_GUARD_MS) {
        if (entry.value) {
          persistedUnread.push(s.id)
        }

        continue
      }

      if (s.unread === true) {
        persistedUnread.push(s.id)
      }
    }

    claim(persistedUnread, 'unread')

    claim(background, 'background')
    // Async delegation: the parent turn has ended but its subagents are still
    // running, so paint real agent work, not a quiet terminal process. This
    // claim yields to `working` the moment the parent turn itself is live.
    claim(delegating, 'delegating')
    // Loaded live turns are resolved below from their runtime owner, not the
    // already lineage-expanded, connection-blind compatibility sets.
    claim(working.filter(id => !scopesByAlias.has(id)), 'working')

    // Stalled REFINES working rather than rivalling it — the turn is still
    // authoritatively running, it has just gone quiet — so it only downgrades a
    // session already claimed as working. The hint outlives its turn by a tick
    // on some paths; without this it could invent a running session.
    for (const id of stalled) {
      for (const alias of claimAliases(id)) {
        if (next[alias] === 'working') {
          next[alias] = 'stalled'
        }
      }
    }

    claim(attention.filter(id => !scopesByAlias.has(id)), 'needs-input')

    // Publish row-qualified answers; bare keys remain only a compatibility
    // lookup for identities that cannot collide. Live owner proof wins over
    // the connection-blind legacy membership sets.
    for (const row of sessions) {
      const aliases = lineageAliases(
        row.id,
        sessions.filter(
          r => backendScopeKey(r.connection_id, r.profile) === backendScopeKey(row.connection_id, row.profile)
        )
      )
      const scopedKey = sessionActivityKey(row)
      for (const [runtimeId, runtime] of Object.entries(states)) {
        if (!runtime.storedSessionId || !aliases.includes(runtime.storedSessionId)) continue
        const owner = runtimeSessionOwner(runtimeId)
        const scope =
          typeof owner === 'string'
            ? backendScopeKey(null, owner)
            : owner
              ? backendScopeKey(owner.connectionId, owner.profile)
              : null
        if (scope && scope !== backendScopeKey(row.connection_id, row.profile)) continue
        if (!scope && scopesByAlias.get(runtime.storedSessionId)?.size !== 1) continue
        if (runtime.busy) next[scopedKey] = stalled.some(id => aliases.includes(id)) ? 'stalled' : 'working'
        if (runtime.needsInput) next[scopedKey] = 'needs-input'
      }
      if (next[scopedKey]) {
        for (const alias of aliases) {
          next[sessionActivityKey({ ...row, id: alias })] = next[scopedKey]
          if (scopesByAlias.get(alias)?.size === 1) next[alias] = next[scopedKey]
        }
      }
    }
    return (dotStates = stableRecord(dotStates, next))
  }
)

/** Listed, non-archived rows whose resolved status is unread. Alias keys in
 *  `$sessionDotStateById` are ignored unless they are themselves a listed row. */
export function unreadSessionCount(
  byId: Readonly<Record<string, SessionDotState>>,
  ...lists: Array<readonly { archived?: boolean; id: string }[]>
): number {
  let n = 0

  for (const rows of lists) {
    for (const row of rows) {
      if (!row.archived && byId[row.id] === 'unread') {
        n++
      }
    }
  }

  return n
}

/** The titlebar badge. Cron sessions are deliberately EXCLUDED: cron runs
 *  finish unwatched by design, so counting them turns the badge into a cron
 *  run counter that is permanently lit (#93552). Their unread state stays
 *  visible where it belongs — the sidebar's cron section rows — and
 *  "mark all as read" still acks them (ackAllSessionsRead iterates cron rows). */
export const $unreadSessionCount = computed(
  [$sessionDotStateById, $sessions, $messagingSessions],
  (byId, sessions, messaging) => unreadSessionCount(byId, sessions, messaging)
)
