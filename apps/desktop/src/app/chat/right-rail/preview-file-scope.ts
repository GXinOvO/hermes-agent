import type { HermesConnection } from '@/global'
import { desktopFsCacheKey, type DesktopFsSessionScope } from '@/lib/desktop-fs'

/** Captured files keep their authority when the ambient window changes. Legacy
 * callers still include that ambient descriptor in their read/edit identity. */
export function previewFileScopeKey(scope: DesktopFsSessionScope, connection: HermesConnection | null) {
  return scope.owner
    ? JSON.stringify([scope.owner.native, scope.owner.connectionId, scope.owner.profile, scope.sessionId])
    : JSON.stringify([desktopFsCacheKey(connection), scope.sessionId])
}
