import { afterEach, expect, it, vi } from 'vitest'

import { setApiRequestConnection } from '@/api/client'
import { setPrimaryGateway, setPrimaryGatewayConnection } from '@/store/gateway'
import { $connection, $selectedStoredSessionId, setSessions } from '@/store/session'
import { $sessionTiles } from '@/store/session-states'

import { captureFileSearchScope, fileSearchOwnerState } from './file-search-owner'

afterEach(() => {
  $connection.set(null)
  $selectedStoredSessionId.set(null)
  $sessionTiles.set([])
  setSessions([])
  setPrimaryGateway(null)
  setPrimaryGatewayConnection(null)
  setApiRequestConnection(null)
  delete document.documentElement.dataset.hermesDesktopHost
  vi.unstubAllGlobals()
})

it('captures a known legacy profile door without inheriting a foreign ambient connection', () => {
  setPrimaryGateway(null, 'legacy-profile')
  setPrimaryGatewayConnection({ connectionId: 'primary', mode: 'remote' }, 'legacy-profile')
  setSessions([{ id: 'legacy', profile: 'legacy-profile' } as never])
  setApiRequestConnection('foreign')
  $connection.set({ connectionId: 'foreign', profile: 'ambient', mode: 'remote' } as never)
  expect(captureFileSearchScope('legacy')).toEqual({
    sessionId: 'legacy', owner: { profile: 'legacy-profile', native: false }
  })
})

it('keeps a local host tile native even when the ambient profile uses SSH', () => {
  $connection.set({ connectionId: 'linux', profile: 'remote', mode: 'remote' } as never)
  setApiRequestConnection('linux')
  $sessionTiles.set([{
    storedSessionId: 'native', runtimeId: 'runtime', workspaceMode: 'sessions',
    ownerRoute: { connectionId: 'local', profile: 'ssh-terminal-profile', mode: 'local' }
  } as never])
  expect(captureFileSearchScope('native')).toEqual({
    sessionId: 'native', owner: { native: true, connectionId: 'local', profile: 'ssh-terminal-profile' }
  })
  document.documentElement.dataset.hermesDesktopHost = 'browser'
  expect(captureFileSearchScope('native').owner?.native).toBe(false)
})

it('does not treat a tagged profile with unknown transport as the ambient native disk', () => {
  setSessions([{ id: 'unknown-mode', profile: 'work', connection_id: 'unresolved' } as never])
  $connection.set({ mode: 'local', profile: 'work' } as never)
  expect(() => captureFileSearchScope('unknown-mode')).toThrow(/filesystem owner is unknown/)
  const state = fileSearchOwnerState('/workspace', 'unknown-mode')
  expect(state.scope).toBeUndefined()
  expect(state.error).toMatch(/unknown-mode/)
})
