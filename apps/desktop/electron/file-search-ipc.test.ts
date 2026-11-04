import { EventEmitter } from 'node:events'

import { describe, expect, it, vi } from 'vitest'

import { registerFileSearchIpc } from './file-search-ipc'

function harness() {
  const handlers = new Map<string, (...args: any[]) => any>()
  const sender = Object.assign(new EventEmitter(), { id: 19, isDestroyed: () => false, send: vi.fn() })

  const runNative = vi.fn(async (_request, options) => {
    options.onFrame({ type: 'batch', hits: [{ path: '/p/a.py', rel: 'a.py', line: 4, col: 1, text: ' x' }] })
    options.onFrame({ type: 'done', truncated: false, warnings: [], elapsedMs: 1 })
  })

  const runRemote = vi.fn(async (_owner, _request, options) => {
    options.onFrame({ type: 'done', truncated: false, warnings: [], elapsedMs: 2 })
  })

  registerFileSearchIpc({
    ipcMain: { handle: (name, fn) => handlers.set(name, fn) },
    runNative,
    runRemote
  })

  return { handlers, sender, runNative, runRemote }
}

const query = { root: '/p', query: 'x', mode: 'content' }

describe('file search IPC authority', () => {
  it('uses the explicit native filesystem instead of a remote connection', async () => {
    const h = harness()
    await h.handlers.get('hermes:fs:search')!({ sender: h.sender }, { id: 'one', native: true, request: query })
    expect(h.runNative).toHaveBeenCalledTimes(1)
    expect(h.runRemote).not.toHaveBeenCalled()
    expect(h.sender.send).toHaveBeenCalledWith(
      'hermes:fs:search:frame',
      expect.objectContaining({ id: 'one', frame: expect.objectContaining({ type: 'batch' }) })
    )
  })

  it('preserves connection/profile/session pinning for remote Docker paths', async () => {
    const h = harness()
    await h.handlers.get('hermes:fs:search')!(
      { sender: h.sender },
      { id: 'one', native: false, connectionId: 'ssh-a', profile: 'work', sessionId: 'docker-session', request: query }
    )
    expect(h.runRemote).toHaveBeenCalledWith(
      { connectionId: 'ssh-a', profile: 'work', sessionId: 'docker-session' },
      expect.objectContaining(query),
      expect.any(Object)
    )
    expect(h.runNative).not.toHaveBeenCalled()
  })

  it.each([null, 7, true, {}, '', ' ', 'bad\0id', 'x'.repeat(4097)])(
    'rejects an invalid session id before any filesystem work: %j', async sessionId => {
      const h = harness()
      await expect(h.handlers.get('hermes:fs:search')!(
        { sender: h.sender }, { id: 'bad', native: false, sessionId, request: query }
      )).rejects.toThrow(/search owner/)
      expect(h.runNative).not.toHaveBeenCalled()
      expect(h.runRemote).not.toHaveBeenCalled()
    }
  )

  it('isolates cancellation by window owner', async () => {
    const h = harness()
    let signal: AbortSignal | undefined
    h.runNative.mockImplementation(async (_request, options) => {
      signal = options.signal
      await new Promise<void>(resolve => options.signal.addEventListener('abort', () => resolve(), { once: true }))
    })

    const pending = h.handlers.get('hermes:fs:search')!(
      { sender: h.sender },
      { id: 'one', native: true, request: query }
    )

    await h.handlers.get('hermes:fs:search:cancel')!({ sender: { id: 20 } }, 'one')
    expect(signal!.aborted).toBe(false)
    await h.handlers.get('hermes:fs:search:cancel')!({ sender: h.sender }, 'one')
    await pending
    expect(signal!.aborted).toBe(true)
  })
})
