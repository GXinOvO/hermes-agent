import http from 'node:http'
import https from 'node:https'

import { htmlResponseError, httpStatusError, jsonAgentFor, withRetry } from './api-transport'
import { DEFAULT_FETCH_TIMEOUT_MS, resolveTimeoutMs } from './hardening'

export function fetchPublicJsonRequest(url: string, options: any = {}, headersForRemoteRequest: (url: string) => Record<string, string>) {
  // Credential-free JSON GET/POST for public gateway endpoints
  // (``/api/status``, ``/api/auth/providers``). Unlike ``fetchJson`` it sends
  // NO ``X-Hermes-Session-Token`` header — used by the auth-mode probe before
  // any credentials exist, and any time we must not leak a token to an
  // endpoint that doesn't need one.
  return withRetry(
    (requestState: any) =>
      new Promise((resolve, reject) => {
        const body = options.body === undefined ? undefined : Buffer.from(JSON.stringify(options.body))
        let parsed

        try {
          parsed = new URL(url)
        } catch (error) {
          reject(new Error(`Invalid URL: ${error.message}`))

          return
        }

        const client = parsed.protocol === 'https:' ? https : http
        const agent = jsonAgentFor(parsed.protocol)
        const timeoutMs = resolveTimeoutMs(options.timeoutMs, DEFAULT_FETCH_TIMEOUT_MS)

        if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
          reject(new Error(`Unsupported Hermes backend URL protocol: ${parsed.protocol}`))

          return
        }

        const req = client.request(
          parsed,
          {
            agent,
            method: options.method || 'GET',
            headers: {
              ...headersForRemoteRequest(url),
              ...(options.headers || {}),
              'Content-Type': 'application/json',
              ...(body ? { 'Content-Length': String(body.length) } : {})
            }
          },
          res => {
            const chunks = []
            res.on('data', chunk => chunks.push(chunk))
            res.on('end', () => {
              const text = Buffer.concat(chunks).toString('utf8')

              if ((res.statusCode || 500) >= 400) {
                reject(httpStatusError(res.statusCode, text, res.statusMessage))

                return
              }

              if (res.statusCode >= 300) {
                reject(htmlResponseError(url, res.statusCode, res.headers.location))

                return
              }

              if (!text) {
                resolve(null)

                return
              }

              const looksHtml = /^\s*<(?:!doctype|html)/i.test(text)
              const contentType = String(res.headers['content-type'] || '')

              if (looksHtml || contentType.includes('text/html')) {
                reject(htmlResponseError(url, res.statusCode))

                return
              }

              try {
                resolve(JSON.parse(text))
              } catch {
                reject(new Error(`Invalid JSON from ${url} (status ${res.statusCode}): ${text.slice(0, 200)}`))
              }
            })
          }
        )

        req.on('error', reject)
        req.setTimeout(timeoutMs, () => {
          req.destroy(new Error(`Timed out connecting to Hermes backend after ${timeoutMs}ms`))
        })

        // Past this point the request is on the wire — see fetchJson.
        requestState.bodySent = true

        if (body) {
          req.write(body)
        }

        req.end()
      }),
    { method: options.method || 'GET' }
  )
}
