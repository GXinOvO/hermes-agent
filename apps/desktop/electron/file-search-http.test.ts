import http from 'node:http'

import { afterEach, describe, expect, it } from 'vitest'

import { streamFileSearch } from './file-search-http'

const servers: http.Server[] = []

afterEach(async () => {
  await Promise.all(
    servers.map(
      server =>
        new Promise<void>(resolve => {
          server.closeAllConnections()
          server.close(() => resolve())
        })
    )
  )
  servers.length = 0
})

async function serve(handler: http.RequestListener) {
  const server = http.createServer(handler)
  servers.push(server)
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))

  return `http://127.0.0.1:${(server.address() as { port: number }).port}/api/fs/search`
}

const request = { mode: 'content' as const, query: '字段', root: '/p' }
const done = { elapsedMs: 1, truncated: false, type: 'done', warnings: [] }

describe('search HTTP stream', () => {
  it('emits batches before response end and decodes split UTF-8', async () => {
    let finish: (() => void) | undefined

    const url = await serve((_req, res) => {
      res.writeHead(200, { 'Content-Type': 'application/x-ndjson' })
      const frame = Buffer.from(JSON.stringify({ type: 'batch', hits: [{ path: '/p/文.py', rel: '文.py' }] }) + '\n')
      const split = frame.indexOf(Buffer.from('文')) + 1
      res.write(frame.subarray(0, split))
      res.write(frame.subarray(split))
      finish = () => res.end(JSON.stringify(done) + '\n')
    })

    let first!: () => void

    const received = new Promise<void>(resolve => {
      first = resolve
    })

    const frames: unknown[] = []

    const pending = streamFileSearch(url, request, {
      signal: new AbortController().signal,
      onFrame: frame => {
        frames.push(frame)
        first()
      }
    })

    await received
    expect(frames).toEqual([{ type: 'batch', hits: [{ path: '/p/文.py', rel: '文.py' }] }])
    finish!()
    await pending
    expect(frames.at(-1)).toEqual(done)
  })

  it('aborts the socket and rejects an unfinished response', async () => {
    let closed!: () => void

    const socketClosed = new Promise<void>(resolve => {
      closed = resolve
    })

    const url = await serve((_req, res) => {
      res.on('close', closed)
      res.writeHead(200, { 'Content-Type': 'application/x-ndjson' })
      res.write(JSON.stringify({ type: 'batch', hits: [] }) + '\n')
    })

    const controller = new AbortController()

    const pending = streamFileSearch(url, request, {
      signal: controller.signal,
      onFrame: () => controller.abort()
    })

    await expect(pending).rejects.toMatchObject({ name: 'AbortError' })
    await socketClosed
  })

  it('reports old backend 404 and rejects a stream without a terminal frame', async () => {
    const old = await serve((_req, res) => {
      res.writeHead(404)
      res.end('Not found')
    })

    await expect(
      streamFileSearch(old, request, { signal: new AbortController().signal, onFrame: () => {} })
    ).rejects.toThrow(/404/)

    const truncated = await serve((_req, res) => {
      res.writeHead(200, { 'Content-Type': 'application/x-ndjson' })
      res.end('{"type":"batch","hits":[]}\n')
    })

    await expect(
      streamFileSearch(truncated, request, { signal: new AbortController().signal, onFrame: () => {} })
    ).rejects.toThrow(/terminal/)
  })
})
