import http from 'node:http'
import https from 'node:https'

import type { FileSearchFrame, FileSearchRequest } from '@hermes/shared/file-search'
import { fileSearchDecoder } from '@hermes/shared/file-search-stream'

import { downloadAgentFor, httpStatusError } from './api-transport'

interface SearchHttpOptions {
  signal: AbortSignal
  onFrame: (frame: FileSearchFrame) => void
  headers?: Record<string, string>
}

/** No redirects or automatic retries: credentials stay on their resolved host,
 * partial streams are never replayed, and an abort closes the very same socket. */
export function streamFileSearch(url: string, request: FileSearchRequest, options: SearchHttpOptions): Promise<void> {
  return new Promise((resolve, reject) => {
    const target = new URL(url)

    if (!['http:', 'https:'].includes(target.protocol)) {
      throw new Error('Unsupported search backend protocol')
    }

    const body = Buffer.from(JSON.stringify(request))
    const reader = fileSearchDecoder(options.onFrame)
    const client = target.protocol === 'https:' ? https : http
    let response: http.IncomingMessage | undefined
    const abortError = () => Object.assign(new Error('Search cancelled'), { name: 'AbortError' })

    const req = client.request(
      target,
      {
        agent: downloadAgentFor(target.protocol),
        method: 'POST',
        headers: { ...options.headers, 'Content-Type': 'application/json', 'Content-Length': String(body.length) }
      },
      res => {
        response = res
        const status = res.statusCode || 500

        if (status !== 200) {
          req.destroy(
            httpStatusError(
              status,
              status === 404 ? 'Update the connected backend to enable rg search' : 'Search backend refused the request'
            )
          )
          res.destroy()

          return
        }

        if (!String(res.headers['content-type']).includes('application/x-ndjson')) {
          req.destroy(new Error('Search backend did not return a search stream'))
          res.destroy()

          return
        }

        res.on('data', chunk => {
          try {
            reader.push(Buffer.from(chunk))
          } catch (error) {
            req.destroy(error as Error)
          }
        })
        res.on('error', reject)
        res.on('aborted', () =>
          reject(options.signal.aborted ? abortError() : new Error('Search response was interrupted'))
        )
        res.on('end', () => {
          try {
            reader.finish()
            resolve()
          } catch (error) {
            reject(error)
          }
        })
      }
    )

    const onAbort = () => req.destroy(abortError())

    const timer = setTimeout(
      () => req.destroy(new Error('Search transport timed out')),
      (request.timeoutMs ?? 15000) + 5000
    )

    const cleanup = () => {
      clearTimeout(timer)
      options.signal.removeEventListener('abort', onAbort)
    }

    req.on('error', error => {
      response?.destroy()
      reject(error)
    })
    req.on('close', cleanup)
    options.signal.addEventListener('abort', onAbort, { once: true })

    if (options.signal.aborted) {
      onAbort()

      return
    }

    req.end(body)
  })
}
