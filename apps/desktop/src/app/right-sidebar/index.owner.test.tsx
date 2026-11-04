import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { sessionFileOwnerScope, setApiRequestConnection } from '@/api/client'
import { group, split } from '@/components/pane-shell/tree/model'
import { $layoutTree, noteActiveTreeGroup } from '@/components/pane-shell/tree/store'
import * as localPreview from '@/lib/local-preview'
import { $previewTabs, closeRightRail } from '@/store/preview'
import { $connection, $selectedStoredSessionId, setSessions } from '@/store/session'
import { $focusedStoredSessionId } from '@/store/session-focus'
import { $focusedWorkspaceCwd, $sessionStates, $sessionTiles } from '@/store/session-states'

import { resetProjectTreeState } from './files/use-project-tree'

import { RightSidebarPane } from './index'

afterEach(() => {
  cleanup()
  resetProjectTreeState()
  $sessionTiles.set([])
  $sessionStates.set({})
  $selectedStoredSessionId.set(null)
  setSessions([])
  $layoutTree.set(null)
  $connection.set(null)
  setApiRequestConnection(null)
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  closeRightRail()
})

it('searches the focused tile root on its own host/profile/session, even in a local window', async () => {
  $connection.set({ connectionId: 'ambient', mode: 'local', profile: 'ambient-profile' } as never)
  setApiRequestConnection('ambient')
  $selectedStoredSessionId.set('primary')
  setSessions([{ id: 'tile-session', cwd: '/workspace', profile: 'tile-profile', connection_id: 'tile-host' } as never])
  $sessionTiles.set([{
    storedSessionId: 'tile-session', runtimeId: 'tile-runtime', workspaceMode: 'sessions',
    ownerRoute: { connectionId: 'tile-host', profile: 'tile-profile', targetProfile: 'remote-tile-profile', mode: 'remote' }
  } as never])
  $sessionStates.set({ 'tile-runtime': { cwd: '/workspace' } as never })
  $layoutTree.set(split('row', [
    group(['files'], { active: 'files', id: 'files-group' }),
    group(['workspace', 'session-tile:tile-session'], { active: 'session-tile:tile-session', id: 'chat-group' })
  ]))
  noteActiveTreeGroup('files-group')
  expect($focusedStoredSessionId.get()).toBe('tile-session')
  expect($focusedWorkspaceCwd.get()).toBe('/workspace')
  expect(sessionFileOwnerScope('tile-session')).toEqual({ connectionId: 'tile-host', profile: 'remote-tile-profile' })

  let listener: (event: any) => void = () => {}

  const searchFiles = vi.fn(async (payload: any) => listener({
    id: payload.id, frame: { type: 'done', truncated: false, warnings: [], elapsedMs: 1 }
  }))

  vi.stubGlobal('hermesDesktop', {
    api: vi.fn(async () => ({ entries: [] })), readDir: vi.fn(async () => ({ entries: [] })),
    searchFiles, cancelFileSearch: async () => {},
    onFileSearchFrame: (callback: typeof listener) => {
      listener = callback

      return () => {}
    }
  })
  render(<RightSidebarPane onActivateFile={() => {}} onActivateFolder={() => {}} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Search files' }))
  fireEvent.change(await screen.findByLabelText('Search file names'), { target: { value: 'needle' } })
  await waitFor(() => expect(searchFiles).toHaveBeenCalledTimes(1))
  expect(searchFiles.mock.calls[0][0]).toMatchObject({
    request: { root: '/workspace' }, native: false,
    connectionId: 'tile-host', profile: 'remote-tile-profile', sessionId: 'tile-session'
  })
})

it('retains the clicked search owner while normalization waits across a focus change', async () => {
  closeRightRail()
  $connection.set({ connectionId: 'ambient', mode: 'local', profile: 'ambient' } as never)
  setApiRequestConnection('ambient')
  $selectedStoredSessionId.set('tile-session')
  setSessions([{ id: 'tile-session', cwd: '/workspace', profile: 'desktop-profile', connection_id: 'tile-host' } as never])
  $sessionTiles.set([{
    storedSessionId: 'tile-session', runtimeId: 'tile-runtime', workspaceMode: 'sessions',
    ownerRoute: { connectionId: 'tile-host', profile: 'desktop-profile', targetProfile: 'docker-profile', mode: 'remote' }
  } as never])
  $layoutTree.set(null)

  let listener: (event: any) => void = () => {}

  vi.stubGlobal('hermesDesktop', {
    readDir: async () => ({ entries: [] }), api: async () => ({ entries: [] }),
    searchFiles: async ({ id }: { id: string }) => {
      listener({ id, frame: { type: 'batch', hits: [{ path: '/workspace/a.py', rel: 'a.py' }] } })
      listener({ id, frame: { type: 'done', truncated: false, warnings: [], elapsedMs: 1 } })
    },
    cancelFileSearch: async () => {},
    onFileSearchFrame: (callback: typeof listener) => {
      listener = callback

      return () => {}
    }
  })

  let release!: (target: ReturnType<typeof localPreview.localPreviewTarget>) => void

  const normalize = vi.spyOn(localPreview, 'normalizeOrLocalPreviewTarget').mockImplementationOnce(
    () => new Promise(resolve => { release = resolve })
  )

  render(<RightSidebarPane onActivateFile={() => {}} onActivateFolder={() => {}} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Search files' }))
  fireEvent.change(await screen.findByLabelText('Search file names'), { target: { value: 'a.py' } })
  fireEvent.click(await screen.findByRole('button', { name: 'a.py' }))
  expect(normalize).toHaveBeenCalledWith('/workspace/a.py', '/workspace', {
    root: '/workspace', sessionId: 'tile-session',
    owner: { connectionId: 'tile-host', profile: 'docker-profile', native: false }
  })
  await act(async () => {
    $selectedStoredSessionId.set('another-session')
    release(localPreview.localPreviewTarget('/workspace/a.py'))
  })
  await waitFor(() => expect($previewTabs.get()).toHaveLength(1))
  expect($previewTabs.get()[0].sessionId).toBe('tile-session')
})
