import { afterEach, describe, expect, it, vi } from 'vitest'

import { createBrowserSearchBridge } from './browser-search'

const bootstrap = { authRequired: true, privateSession: false, basePath: '/hermes', token: '' }

const payload = {
  id: 'query',
  native: false,
  connectionId: 'local',
  profile: 'work',
  sessionId: 'docker/session + one',
  request: {
    root: '/project',
    query: 'field',
    mode: 'content' as const
  }
}

afterEach(() => vi.unstubAllGlobals())

describe('browser search bridge', () => {
  it('streams from the same-origin profile route with existing authentication', async () => {
    const frames: unknown[] = []

    const fetchMock = vi.fn(
      async () =>
        new Response('{"type":"done","truncated":false,"warnings":[],"elapsedMs":1}\n', {
          headers: { 'Content-Type': 'application/x-ndjson' }
        })
    )

    vi.stubGlobal('fetch', fetchMock)
    const bridge = createBrowserSearchBridge(bootstrap)
    const unsubscribe = bridge.onFileSearchFrame!(frame => frames.push(frame))
    await bridge.searchFiles!(payload)
    const [url, options] = fetchMock.mock.calls[0] as unknown as [URL, RequestInit]
    expect(url.origin).toBe(window.location.origin)
    expect(url.pathname).toBe('/hermes/api/fs/search')
    expect(url.searchParams.get('profile')).toBe('work')
    expect(url.searchParams.get('session_id')).toBe(payload.sessionId)
    expect(options.credentials).toBe('same-origin')
    expect(JSON.parse(options.body as string)).toEqual(payload.request)
    expect(frames).toHaveLength(1)
    unsubscribe()
  })

  it('does not route a foreign connection or native path to the web server', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const bridge = createBrowserSearchBridge(bootstrap)
    await expect(bridge.searchFiles!({ ...payload, connectionId: 'foreign' })).rejects.toThrow(/No connection/)
    await expect(bridge.searchFiles!({ ...payload, native: true })).rejects.toThrow(/connected filesystem/)
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('reports interrupted responses instead of no matches', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async () =>
          new Response('{"type":"batch","hits":[]}\n', {
            headers: { 'Content-Type': 'application/x-ndjson' }
          })
      )
    )
    const bridge = createBrowserSearchBridge(bootstrap)
    await expect(bridge.searchFiles!(payload)).rejects.toThrow(/terminal/)
  })

  it('surfaces a session ownership rejection without retrying an unscoped search', async () => {
    const fetchMock = vi.fn(async () => new Response('Session does not belong to this profile', { status: 404 }))
    vi.stubGlobal('fetch', fetchMock)
    const bridge = createBrowserSearchBridge(bootstrap)
    await expect(bridge.searchFiles!(payload)).rejects.toThrow(/404/)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect((fetchMock.mock.calls[0] as unknown as [URL])[0].searchParams.get('session_id')).toBe(payload.sessionId)
  })
})
