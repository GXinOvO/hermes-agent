import { afterEach, expect, it, vi } from 'vitest'

import { setApiRequestConnection } from '@/api/client'
import { $connection } from '@/store/session'

import { type DesktopFsSessionScope, readDesktopFileDataUrl, readDesktopFileText, writeDesktopFileText } from './desktop-fs'

const scope: DesktopFsSessionScope = {
  sessionId: 'docker-one', owner: { connectionId: 'tile-host', profile: 'tile-profile', native: false }
}

function bridge() {
  const api = vi.fn(async ({ path }: { path: string }) => path.includes('read-data-url')
    ? { dataUrl: 'data:text/plain;base64,cmVtb3Rl' }
    : { path: '/workspace/a.txt', text: 'remote' })

  const readFileText = vi.fn(async () => ({ path: 'C:\\repo\\a.txt', text: 'native' }))
  const readFileDataUrl = vi.fn(async () => 'data:text/plain;base64,bmF0aXZl')
  const writeTextFile = vi.fn(async (path: string) => ({ path }))
  vi.stubGlobal('hermesDesktop', { api, readFileText, readFileDataUrl, writeTextFile })

  return { api, readFileText, readFileDataUrl, writeTextFile }
}

afterEach(() => {
  $connection.set(null)
  setApiRequestConnection(null)
  vi.unstubAllGlobals()
})

it('uses captured remote authority for reads and Save in an ambient native window', async () => {
  const h = bridge()
  $connection.set({ connectionId: 'local', mode: 'local', profile: 'ambient' } as never)
  setApiRequestConnection('local')
  await expect(readDesktopFileText('/workspace/a.txt', scope)).resolves.toMatchObject({ text: 'remote' })
  await expect(readDesktopFileDataUrl('/workspace/a.txt', scope)).resolves.toBe('data:text/plain;base64,cmVtb3Rl')
  await writeDesktopFileText('/workspace/a.txt', 'saved', scope)
  expect(h.api.mock.calls.map(([request]) => request)).toEqual([
    { connectionId: 'tile-host', profile: 'tile-profile', path: '/api/fs/read-text?path=%2Fworkspace%2Fa.txt&session_id=docker-one' },
    { connectionId: 'tile-host', profile: 'tile-profile', path: '/api/fs/read-data-url?path=%2Fworkspace%2Fa.txt&session_id=docker-one' },
    { connectionId: 'tile-host', profile: 'tile-profile', method: 'POST', path: '/api/fs/write-text', body: { path: '/workspace/a.txt', content: 'saved', session_id: 'docker-one' } }
  ])
  expect(h.readFileText).not.toHaveBeenCalled()
  expect(h.readFileDataUrl).not.toHaveBeenCalled()
  expect(h.writeTextFile).not.toHaveBeenCalled()
})

it('keeps explicit native Windows paths on Electron after the window switches to remote', async () => {
  const h = bridge()
  $connection.set({ connectionId: 'remote', mode: 'remote', profile: 'ssh-override' } as never)
  setApiRequestConnection('remote')
  const local: DesktopFsSessionScope = { sessionId: 'local-chat', owner: { native: true, connectionId: 'local', profile: 'work' } }
  const path = 'C:\\repo\\a.txt'
  await expect(readDesktopFileText(path, local)).resolves.toMatchObject({ text: 'native' })
  await readDesktopFileDataUrl(path, local)
  await writeDesktopFileText(path, 'saved', local)
  expect(h.readFileText).toHaveBeenCalledWith(path)
  expect(h.readFileDataUrl).toHaveBeenCalledWith(path)
  expect(h.writeTextFile).toHaveBeenCalledWith(path, 'saved')
  expect(h.api).not.toHaveBeenCalled()
})
