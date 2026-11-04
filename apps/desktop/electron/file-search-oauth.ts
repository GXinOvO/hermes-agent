import type { FileSearchFrame, FileSearchRequest } from '@hermes/shared/file-search'
import { fileSearchDecoder } from '@hermes/shared/file-search-stream'
import type { ClientRequest, IncomingMessage, Session } from 'electron'

import { httpStatusError } from './api-transport'
import { remoteSessionCookies } from './remote-session-cookies'

/** Use the same Electron session/cookie partition as ordinary remote API calls. */
export function streamOauthFileSearch(
  url: string,
  body: FileSearchRequest,
  options: {
    signal: AbortSignal
    onFrame: (frame: FileSearchFrame) => void
    headers: Record<string, string>
    session: Session
    partition?: string | null
    request: (options: { method: string; url: string; session: Session; redirect: 'error'; useSessionCookies: true }) => ClientRequest
  }
): Promise<void> {
  return new Promise((resolve, reject) => {
    const req = options.request({ method: 'POST', url, session: options.session, redirect: 'error', useSessionCookies: true })
    const decoder = fileSearchDecoder(options.onFrame)

    const abort = () => {
      cleanup()
      req.abort()
      reject(Object.assign(new Error('Search cancelled'), { name: 'AbortError' }))
    }

    const timer = setTimeout(
      () => {
        req.abort()
        reject(new Error('Search transport timed out'))
      },
      (body.timeoutMs ?? 15000) + 5000
    )

    const cleanup = () => {
      clearTimeout(timer)
      options.signal.removeEventListener('abort', abort)
    }

    req.on('error', error => {
      cleanup()
      reject(error)
    })
    req.on('response', (res: IncomingMessage) => {
      if (options.partition) {remoteSessionCookies.record(options.partition, url, res.headers['set-cookie'])}

      const fail = (error: Error) => {
        cleanup()
        req.abort()
        reject(error)
      }

      if (res.statusCode !== 200 || !String(res.headers['content-type']).includes('application/x-ndjson')) {
        fail(httpStatusError(res.statusCode, 'Search is unavailable on the connected backend'))

        return
      }

      res.on('error', fail)
      res.on('data', (chunk: Buffer) => {
        try {
          decoder.push(chunk)
        } catch (error) {
          fail(error as Error)
        }
      })
      res.on('end', () => {
        cleanup()

        try {
          decoder.finish()
          resolve()
        } catch (error) {
          reject(error)
        }
      })
    })

    const headers = { ...options.headers }
    const mirrored = options.partition ? remoteSessionCookies.cookieHeaderFor(options.partition, url) : null

    if (mirrored && !Object.keys(headers).some(name => name.toLowerCase() === 'cookie')) {headers.Cookie = mirrored}

    for (const [name, value] of Object.entries(headers)) {
      req.setHeader(name, value)
    }

    req.setHeader('Content-Type', 'application/json')
    options.signal.addEventListener('abort', abort, { once: true })

    if (options.signal.aborted) {
      abort()
      cleanup()

      return
    }

    req.write(JSON.stringify(body))
    req.end()
  })
}
