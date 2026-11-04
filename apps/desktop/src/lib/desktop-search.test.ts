import type { FileSearchFrame } from '@hermes/shared/file-search'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { setApiRequestConnection } from '@/api/client'
import { $connection } from '@/store/session'

import type { DesktopFsSessionScope } from './desktop-fs'
import { searchDesktopFiles } from './desktop-search'

const request = { root: '/project', query: '字段', mode: 'content' as const }

function bridge() {
  let listener: (event: { id: string; frame: FileSearchFrame }) => void = () => {}
  const unsubscribe = vi.fn()
  const cancelFileSearch = vi.fn(async () => {})

  const searchFiles = vi.fn(async ({ id }: { id: string }) => {
    listener({ id: 'different', frame: { type: 'error', code: 'old', message: 'stale' } })
    listener({ id, frame: { type: 'done', truncated: false, warnings: [], elapsedMs: 1 } })
  })

  vi.stubGlobal('hermesDesktop', {
    searchFiles,
    cancelFileSearch,
    onFileSearchFrame: (value: typeof listener) => {
      listener = value

      return unsubscribe
    }
  })

  return {
    searchFiles,
    cancelFileSearch,
    unsubscribe,
    send: (event: Parameters<typeof listener>[0]) => listener(event)
  }
}

afterEach(() => {
  vi.unstubAllGlobals()
  $connection.set(null)
  setApiRequestConnection(null)
})

describe('desktop search owner and request lifecycle', () => {
  it('pins remote connection/profile and drops another query frame', async () => {
    const h = bridge()
    $connection.set({ mode: 'remote', profile: 'work' } as any)
    setApiRequestConnection('linux-host')
    const onFrame = vi.fn()
    await searchDesktopFiles(request, { signal: new AbortController().signal, onFrame })
    expect(h.searchFiles).toHaveBeenCalledWith(
      expect.objectContaining({
        native: false,
        connectionId: 'linux-host',
        profile: 'work',
        request
      })
    )
    expect(onFrame).toHaveBeenCalledTimes(1)
    expect(onFrame.mock.calls[0][0].type).toBe('done')
    expect(h.unsubscribe).toHaveBeenCalledOnce()
  })

  it('aborts by exact id and drops post-cancel frames', async () => {
    const h = bridge()
    let release!: () => void
    h.searchFiles.mockImplementation(async ({ id }) => {
      await new Promise<void>(resolve => {
        release = resolve
      })
      h.send({ id, frame: { type: 'done', truncated: false, warnings: [], elapsedMs: 1 } })
    })
    const controller = new AbortController()
    const onFrame = vi.fn()
    const pending = searchDesktopFiles(request, { signal: controller.signal, onFrame })
    const id = (h.searchFiles.mock.calls[0][0] as { id: string }).id
    controller.abort()
    expect(h.cancelFileSearch).toHaveBeenCalledWith(id)
    release()
    await pending
    expect(onFrame).not.toHaveBeenCalled()
    expect(h.unsubscribe).toHaveBeenCalledOnce()
  })

  it('does not turn an old bridge or missing terminal response into no matches', async () => {
    vi.stubGlobal('hermesDesktop', {})
    await expect(
      searchDesktopFiles(request, { signal: new AbortController().signal, onFrame: vi.fn() })
    ).rejects.toThrow(/rebuild/)
    const h = bridge()
    h.searchFiles.mockImplementation(async () => {})
    await expect(
      searchDesktopFiles(request, { signal: new AbortController().signal, onFrame: vi.fn() })
    ).rejects.toThrow(/terminal/)
  })

  it('uses the captured native authority and preserves Windows path/query bytes', async () => {
    const h = bridge()
    $connection.set({ mode: 'remote', profile: 'remote' } as never)
    setApiRequestConnection('remote')

    const scope: DesktopFsSessionScope = {
      sessionId: 'native-session', owner: { native: true, connectionId: 'local', profile: 'ssh-terminal' }
    }

    const windows = { root: 'C:\\Users\\User\\repo', query: 'a[1].py', mode: 'name' as const, regex: false }

    await searchDesktopFiles(windows, { scope, signal: new AbortController().signal, onFrame: vi.fn() })
    expect(h.searchFiles).toHaveBeenCalledWith(expect.objectContaining({
      native: true, connectionId: 'local', profile: 'ssh-terminal', sessionId: 'native-session', request: windows
    }))
  })
})
