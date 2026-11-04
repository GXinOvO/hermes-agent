import { EventEmitter } from 'node:events'

import { afterEach, describe, expect, it, vi } from 'vitest'

import { streamOauthFileSearch } from './file-search-oauth'
import { remoteSessionCookies } from './remote-session-cookies'

afterEach(() => remoteSessionCookies.clear())

function transport() {
  const req = Object.assign(new EventEmitter(), { abort: vi.fn(), setHeader: vi.fn(), write: vi.fn(), end: vi.fn() })
  const request = vi.fn(() => req)

  const res = Object.assign(new EventEmitter(), {
    statusCode: 200,
    headers: { 'content-type': 'application/x-ndjson' } as Record<string, string | string[]>
  })

  return { req, request, res }
}

describe('OAuth search transport', () => {
  it('uses its cookie session, streams frames, and cleans up after end', async () => {
    const h = transport()
    const session = {} as any
    const onFrame = vi.fn()

    const pending = streamOauthFileSearch(
      'https://hermes.example/api/fs/search',
      { root: '/p', query: 'x', mode: 'content' },
      {
        signal: new AbortController().signal,
        session,
        request: h.request as any,
        headers: { 'X-Custom': 'yes' },
        onFrame
      }
    )

    expect(h.request).toHaveBeenCalledWith(expect.objectContaining({ session, redirect: 'error', method: 'POST', useSessionCookies: true }))
    expect(h.req.setHeader).toHaveBeenCalledWith('X-Custom', 'yes')
    h.req.emit('response', h.res)
    h.res.emit('data', Buffer.from('{"type":"batch","hits":[]}\n'))
    expect(onFrame).toHaveBeenCalledOnce()
    h.res.emit('data', Buffer.from('{"type":"done","truncated":false,"warnings":[],"elapsedMs":1}\n'))
    h.res.emit('end')
    await pending
  })

  it('uses only the resolved partition mirror and records response cookies without overriding explicit cookies', async () => {
    remoteSessionCookies.record('selected', 'https://hermes.example/', 'hermes_session=synthetic; Path=/')
    remoteSessionCookies.record('other', 'https://hermes.example/', 'hermes_session=wrong; Path=/')

    for (const explicit of [undefined, 'caller=synthetic']) {
      const h = transport()
      const headers: Record<string, string> = explicit ? { cookie: explicit } : {}

      const pending = streamOauthFileSearch('https://hermes.example/api/fs/search', { root: '/p', query: 'x', mode: 'content' }, {
        signal: new AbortController().signal, partition: 'selected', session: {} as any,
        request: h.request as any, headers, onFrame: vi.fn()
      })

      expect(h.req.setHeader).toHaveBeenCalledWith(explicit ? 'cookie' : 'Cookie', explicit || 'hermes_session=synthetic')
      expect(h.req.setHeader).not.toHaveBeenCalledWith('Cookie', 'hermes_session=wrong')
      h.res.headers['set-cookie'] = ['next_cookie=synthetic; Path=/api']
      h.req.emit('response', h.res)
      h.res.emit('data', Buffer.from('{"type":"done","truncated":false,"warnings":[],"elapsedMs":1}\n'))
      h.res.emit('end')
      await pending
      expect(remoteSessionCookies.cookieHeaderFor('selected', 'https://hermes.example/api/fs/search')).toContain('next_cookie=synthetic')
      expect(remoteSessionCookies.cookieHeaderFor('selected', 'https://other.example/api/fs/search')).toBeNull()
    }
  })

  it('aborts the actual request and preserves the cancellation error', async () => {
    const h = transport()
    const controller = new AbortController()

    const pending = streamOauthFileSearch(
      'https://hermes.example/api/fs/search',
      { root: '/p', query: 'x', mode: 'content' },
      {
        signal: controller.signal,
        session: {} as any,
        request: h.request as any,
        headers: {},
        onFrame: vi.fn()
      }
    )

    controller.abort()
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' })
    expect(h.req.abort).toHaveBeenCalledOnce()
  })
})
