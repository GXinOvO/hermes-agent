import type { SessionTile } from './session-states'

export type StoredTile = Pick<SessionTile,
  'anchor' | 'before' | 'dir' | 'exactReference' | 'ownerProfile' | 'ownerRoute' | 'storedSessionId' |
  'workspaceMode' | 'workspaceOwnerKey' | 'workspaceTabTitle'
>

export const toStored = (t: SessionTile): StoredTile => ({
  anchor: t.anchor,
  before: t.before,
  dir: t.dir,
  ...(t.exactReference ? { exactReference: t.exactReference } : {}),
  ...(t.ownerProfile ? { ownerProfile: t.ownerProfile } : {}),
  ...(t.ownerRoute ? { ownerRoute: t.ownerRoute } : {}),
  storedSessionId: t.storedSessionId,
  ...(t.workspaceMode ? { workspaceMode: t.workspaceMode } : {}),
  ...(t.workspaceOwnerKey ? { workspaceOwnerKey: t.workspaceOwnerKey } : {}),
  ...(t.workspaceTabTitle ? { workspaceTabTitle: t.workspaceTabTitle } : {})
})
