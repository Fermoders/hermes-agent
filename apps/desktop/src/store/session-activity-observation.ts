/** Renderer observation order, shared by list/info rows and live rosters.
 * A retained row is a cache, not permanent authority over newer lifecycle facts. */
import { atom } from 'nanostores'
export const $sessionActivityObservationEpoch = atom(0)
let revision = 0
const observations = new WeakMap<object, number>()

export function observeSessionActivity(value: object): number {
  const next = ++revision
  observations.set(value, next)

  return next
}

export function sessionActivityObservation(value: object): number {
  return observations.get(value) ?? 0
}
