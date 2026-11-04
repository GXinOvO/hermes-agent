import { describe, expect, it } from 'vitest'

import { fileSearchApiRequest } from './file-search-api-request'

describe('file search session request', () => {
  it('carries the captured connection/profile/session into the existing API resolver', () => {
    const run = { signal: new AbortController().signal, onFrame: () => {} }
    const body = { root: '/workspace', query: 'needle', mode: 'content' as const }
    const request = fileSearchApiRequest({ connectionId: 'tile-host', profile: 'tile-profile', sessionId: 'session /?&' }, body, run)
    expect(request).toEqual({
      connectionId: 'tile-host', profile: 'tile-profile', path: '/api/fs/search?session_id=session%20%2F%3F%26',
      method: 'POST', body, searchStream: run, timeoutMs: 20000
    })
  })

  it('leaves host searches without a fabricated session', () => {
    const run = { signal: new AbortController().signal, onFrame: () => {} }
    const request = fileSearchApiRequest({ profile: 'default' }, { root: '/project', query: 'needle', mode: 'name' }, run)
    expect(request.path).toBe('/api/fs/search')
    expect(request.connectionId).toBeUndefined()
  })
})
