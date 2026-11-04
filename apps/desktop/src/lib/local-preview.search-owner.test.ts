import { afterEach, expect, it, vi } from 'vitest'

import { setApiRequestConnection } from '@/api/client'
import { $connection } from '@/store/session'

import type { DesktopFsSessionScope } from './desktop-fs'
import { normalizeOrLocalPreviewTarget } from './local-preview'

const scope: DesktopFsSessionScope = {
  sessionId: 'one', owner: { connectionId: 'tile', profile: 'docker', native: false }
}

afterEach(() => {
  $connection.set(null)
  setApiRequestConnection(null)
  vi.unstubAllGlobals()
})

it('does not normalize a captured remote path against the local disk or store its canonical host path', async () => {
  const normalizePreviewTarget = vi.fn(async () => ({
    kind: 'file', label: 'a.py', path: '/local/canonical/a.py', source: '/local/canonical/a.py',
    url: 'file:///local/canonical/a.py', previewKind: 'text'
  }))

  const readFileText = vi.fn(async () => ({ text: 'wrong local file' }))
  const api = vi.fn(async () => ({ path: '/host/docker-canonical/a.py', text: 'remote' }))
  vi.stubGlobal('hermesDesktop', { normalizePreviewTarget, readFileText, api })
  $connection.set({ mode: 'local', profile: 'ambient' } as never)
  setApiRequestConnection('local')
  const result = await normalizeOrLocalPreviewTarget('a.py', '/workspace', scope)
  expect(normalizePreviewTarget).not.toHaveBeenCalled()
  expect(readFileText).not.toHaveBeenCalled()
  expect(api).toHaveBeenCalledWith({
    connectionId: 'tile', profile: 'docker', path: '/api/fs/read-text?path=%2Fworkspace%2Fa.py&session_id=one'
  })
  expect(result).toMatchObject({ path: '/workspace/a.py', url: 'file:///workspace/a.py', fileScope: scope })
})

it('keeps native normalization authoritative for a captured Windows path in a remote window', async () => {
  const normalizePreviewTarget = vi.fn(async () => ({
    kind: 'file', path: 'C:\\repo\\a.py', source: 'C:\\repo\\a.py', url: 'file:///C:/repo/a.py', label: 'a.py', previewKind: 'text'
  }))

  const api = vi.fn()
  vi.stubGlobal('hermesDesktop', { normalizePreviewTarget, api })
  $connection.set({ mode: 'remote', profile: 'remote' } as never)
  const local: DesktopFsSessionScope = { sessionId: 'local-session', owner: { native: true, connectionId: 'local', profile: 'work' } }
  const result = await normalizeOrLocalPreviewTarget('C:\\repo\\a.py', 'C:\\repo', local)
  expect(normalizePreviewTarget).toHaveBeenCalledWith('C:\\repo\\a.py', 'C:\\repo')
  expect(api).not.toHaveBeenCalled()
  expect(result).toMatchObject({ path: 'C:\\repo\\a.py', fileScope: local })
})
