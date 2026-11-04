import { computed } from 'nanostores'

import { $apiRequestScope, connectionScoped, sessionFileOwnerScope } from '@/api/client'
import type { DesktopFsSessionScope } from '@/lib/desktop-fs'
import { desktopFsProfile, isDesktopFsRemoteMode } from '@/lib/desktop-fs'
import { hostHas } from '@/lib/host-capabilities'
import { $connectionsRegistry } from '@/store/connection-registry-state'
import { $primaryGatewayRoute } from '@/store/gateway-primary-route'
import { $connection, $cronSessions, $messagingSessions, $sessions, $unlistedSessionOwnerRows } from '@/store/session'
import { $focusedStoredSessionId } from '@/store/session-focus'
import { $sessionTiles, knownOwnerForSession, ownerConnectionMode } from '@/store/session-states'

export interface FileSearchScope extends DesktopFsSessionScope {
  root: string
}

/** Reactive inputs of the existing owner resolver, including same-id route
 * changes. A derived store keeps React Compiler from memoizing hidden reads. */
function fileSearchOwnerStore() {
  let previous = fileSearchOwnerState('', null)

  return computed(
    [
      $focusedStoredSessionId, $connection, $apiRequestScope, $sessionTiles,
      $sessions, $cronSessions, $messagingSessions, $unlistedSessionOwnerRows,
      $connectionsRegistry, $primaryGatewayRoute
    ],
    sessionId => {
      const next = fileSearchOwnerState('', sessionId)

      // List refreshes with unchanged authority must not cancel a running query.
      if (next.key !== previous.key) {
        previous = next
      }

      return previous
    }
  )
}

export const $fileSearchOwner = fileSearchOwnerStore()

export function fileSearchOwnerState(root: string, sessionId: null | string) {
  try {
    const scope: FileSearchScope = { ...captureFileSearchScope(sessionId), root }

    return { scope, key: fileSearchScopeKey(scope), error: '' }
  } catch (cause) {
    const error = cause instanceof Error ? cause.message : String(cause)

    return { scope: undefined, key: error, error }
  }
}

/** Freeze the route when intent is captured, including same-ambient owners: a
 * later connection switch must not move a session's Docker workspace. */
export function captureFileSearchScope(sessionId: null | string = $focusedStoredSessionId.get()): DesktopFsSessionScope {
  if (!sessionId) {
    return { owner: { ...connectionScoped(), profile: desktopFsProfile(), native: !isDesktopFsRemoteMode() } }
  }

  const known = knownOwnerForSession(sessionId)
  const fileOwner = sessionFileOwnerScope(sessionId)
  const mode = ownerConnectionMode(known)

  if (!known || !fileOwner || !mode) {
    throw new Error(`Cannot search: filesystem owner is unknown for session ${sessionId}`)
  }

  return {
    sessionId,
    owner: {
      ...fileOwner,
      connectionId: typeof known === 'object' ? known.connectionId : undefined,
      native: mode === 'local' && hostHas('nativeHostFiles')
    }
  }
}

export function fileSearchScopeKey(scope: DesktopFsSessionScope): string {
  return JSON.stringify([scope.owner?.native, scope.owner?.connectionId, scope.owner?.profile, scope.sessionId])
}
