import http from 'node:http'

import { afterEach, describe, expect, it } from 'vitest'

import { fetchPublicJsonRequest } from './api-public-json'
import { multipartBody } from './api-request-body'

const servers: http.Server[] = []

afterEach(async () => {
  await Promise.all(servers.splice(0).map(server => new Promise<void>(resolve => {
    server.closeAllConnections()
    server.close(() => resolve())
  })))
})

async function serve(handler: http.RequestListener) {
  const server = http.createServer(handler)
  servers.push(server)
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))

  return `http://127.0.0.1:${(server.address() as { port: number }).port}`
}

describe('extracted public JSON transport', () => {
  it('preserves configured headers and JSON POST without adding a session token', async () => {
    let received: unknown

    const origin = await serve((req, res) => {
      let body = ''
      req.on('data', chunk => { body += chunk })
      req.on('end', () => {
        received = { method: req.method, custom: req.headers['x-custom'], session: req.headers['x-hermes-session-token'], body: JSON.parse(body) }
        res.setHeader('Content-Type', 'application/json')
        res.end('{"ok":true}')
      })
    })

    const value = await fetchPublicJsonRequest(origin, { method: 'POST', body: { field: '文字' } }, () => ({ 'X-Custom': 'configured' }))
    expect(value).toEqual({ ok: true })
    expect(received).toEqual({ method: 'POST', custom: 'configured', session: undefined, body: { field: '文字' } })
  })

  it('keeps HTML/redirect/error statuses explicit', async () => {
    const origin = await serve((req, res) => {
      if (req.url === '/redirect') { res.writeHead(302, { Location: '/login' }); res.end();

 return }

      if (req.url === '/error') { res.writeHead(403); res.end('denied');

 return }

      res.setHeader('Content-Type', 'text/html')
      res.end('<html>login</html>')
    })

    await expect(fetchPublicJsonRequest(origin, {}, () => ({}))).rejects.toThrow(/HTML|login|JSON/)
    await expect(fetchPublicJsonRequest(origin + '/redirect', {}, () => ({}))).rejects.toThrow(/302/)
    await expect(fetchPublicJsonRequest(origin + '/error', {}, () => ({}))).rejects.toMatchObject({ statusCode: 403 })
  })

  it('preserves multipart bytes and strips filename header injection', () => {
    const bytes = Buffer.from([0, 1, 255])
    const result = multipartBody({ filename: 'a"\r\nbin', bytes, contentType: 'application/octet-stream' })
    const boundary = result.contentType.split('boundary=')[1]
    expect(result.body.includes(bytes)).toBe(true)
    expect(result.body.toString('latin1')).toContain('filename="a___bin"')
    expect(result.body.toString('latin1')).toContain(`--${boundary}--\r\n`)
  })
})
