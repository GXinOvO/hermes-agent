import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { setApiRequestConnection } from '@/api/client'
import type { HermesApiRequest, HermesConnection } from '@/global'
import type { DesktopFsSessionScope } from '@/lib/desktop-fs'
import type { PreviewTarget } from '@/store/preview'
import { $connection } from '@/store/session'

import { readTextPreview, useLocalPreviewLoad } from './preview-file-load'

const remote: DesktopFsSessionScope = {
  owner: { native: false, connectionId: 'file-host', profile: 'file-profile' }, sessionId: 'sandbox-a'
}

const file: PreviewTarget = {
  kind: 'file', label: 'a.py', path: '/workspace/a.py', source: '/workspace/a.py',
  url: 'file:///workspace/a.py', previewKind: 'text', fileScope: remote
}

const options = { target: file, forcePreview: false, reloadKey: 0, selfReload: 0 }

function ambient(connection: HermesConnection) {
  $connection.set(connection)
  setApiRequestConnection(connection.connectionId || null)
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  $connection.set(null)
  setApiRequestConnection(null)
})

describe('captured search file preview owner', () => {
  it('reads remote source through its captured authority and skips ambient git enrichment', async () => {
    ambient({ mode: 'local', connectionId: 'local', profile: 'ambient' } as HermesConnection)

    const api = vi.fn(async (request: HermesApiRequest) => {
      expect(request.connectionId).toBe('file-host')
      expect(request.profile).toBe('file-profile')
      expect(request.path).toContain('session_id=sandbox-a')

      return { text: 'remote source', path: '/canonical/host/a.py' }
    })

    const native = vi.fn()
    const gitRoot = vi.fn()
    vi.stubGlobal('hermesDesktop', { api, readFileText: native, gitRoot })
    const rendered = renderHook(useLocalPreviewLoad, { initialProps: options })
    await waitFor(() => expect(rendered.result.current.state.text).toBe('remote source'))
    expect(rendered.result.current.filePath).toBe('/workspace/a.py')
    expect(rendered.result.current.navigationReady).toBe(true)
    expect(api).toHaveBeenCalledOnce()
    expect(native).not.toHaveBeenCalled()
    expect(gitRoot).not.toHaveBeenCalled()
    act(() => ambient({ mode: 'remote', connectionId: 'other', profile: 'different' } as HermesConnection))
    expect(api).toHaveBeenCalledOnce()
    expect(rendered.result.current.navigationReady).toBe(true)
  })

  it.each(['connection', 'profile', 'session'] as const)('rejects old same-path reads after a captured %s change', async field => {
    let release!: (result: { text: string }) => void
    const pending = new Promise<{ text: string }>(resolve => { release = resolve })
    const api = vi.fn().mockReturnValueOnce(pending).mockResolvedValueOnce({ text: 'new owner' })
    vi.stubGlobal('hermesDesktop', { api, readFileText: vi.fn(), gitRoot: vi.fn() })
    ambient({ mode: 'local', connectionId: 'local' } as HermesConnection)
    const rendered = renderHook(useLocalPreviewLoad, { initialProps: options })

    const scope: DesktopFsSessionScope = {
      owner: { ...remote.owner!, ...(field === 'connection' ? { connectionId: 'other-host' } : {}), ...(field === 'profile' ? { profile: 'other-profile' } : {}) },
      sessionId: field === 'session' ? 'sandbox-b' : remote.sessionId
    }

    rendered.rerender({ ...options, target: { ...file, fileScope: scope } })
    await waitFor(() => expect(rendered.result.current.state.text).toBe('new owner'))
    await act(async () => release({ text: 'old owner' }))
    expect(rendered.result.current.state.text).toBe('new owner')
    expect(rendered.result.current.navigationReady).toBe(true)
    expect(api).toHaveBeenCalledTimes(2)
  })

  it('keeps native Windows file authority when the ambient connection is remote', async () => {
    const path = 'C:\\project\\a.py'
    const native = vi.fn(async () => ({ path, text: 'native source' }))
    const api = vi.fn()
    vi.stubGlobal('hermesDesktop', { api, readFileText: native, gitRoot: vi.fn() })
    ambient({ mode: 'remote', connectionId: 'linux-host', profile: 'remote' } as HermesConnection)
    const target: PreviewTarget = { ...file, path, source: path, url: 'file:///C:/project/a.py', fileScope: { owner: { native: true }, sessionId: 'local-session' } }
    const rendered = renderHook(useLocalPreviewLoad, { initialProps: { ...options, target } })
    await waitFor(() => expect(rendered.result.current.state.text).toBe('native source'))
    expect(native).toHaveBeenCalledExactlyOnceWith(path)
    expect(api).not.toHaveBeenCalled()
  })

  it.each(['image', 'pdf'] as const)('loads %s bytes from its captured gateway instead of the ambient native disk', async kind => {
    const api = vi.fn(async (request: HermesApiRequest) => {
      expect(request.connectionId).toBe('file-host')
      expect(request.profile).toBe('file-profile')
      expect(request.path).toContain('/read-data-url?')
      expect(request.path).toContain('session_id=sandbox-a')

      return { dataUrl: 'data:application/octet-stream;base64,cmVtb3Rl' }
    })

    const native = vi.fn()
    vi.stubGlobal('hermesDesktop', { api, readFileText: native, readFileDataUrl: native })
    ambient({ mode: 'local', connectionId: 'local' } as HermesConnection)
    const rendered = renderHook(useLocalPreviewLoad, { initialProps: { ...options, target: { ...file, previewKind: kind } } })
    await waitFor(() => expect(rendered.result.current.state.dataUrl).toBe('data:application/octet-stream;base64,cmVtb3Rl'))
    expect(api).toHaveBeenCalledOnce()
    expect(native).not.toHaveBeenCalled()
  })

  it('uses the captured gateway for the missing-handler byte fallback', async () => {
    const api = vi.fn(async (request: HermesApiRequest) => {
      expect(request.connectionId).toBe('file-host')
      expect(request.profile).toBe('file-profile')
      expect(request.path).toContain('session_id=sandbox-a')

      if (request.path.includes('/read-text?')) {throw new Error("No handler registered for 'hermes:readFileText'")}
      expect(request.path).toContain('/read-data-url?')

      return { dataUrl: 'data:text/plain;base64,cmVtb3Rl' }
    })

    const native = vi.fn()
    vi.stubGlobal('hermesDesktop', { api, readFileText: native, readFileDataUrl: native })
    ambient({ mode: 'local', connectionId: 'local' } as HermesConnection)
    const result = await readTextPreview('/workspace/a.py', undefined, remote)
    expect(result.text).toBe('remote')
    expect(api).toHaveBeenCalledTimes(2)
    expect(native).not.toHaveBeenCalled()
  })
})
