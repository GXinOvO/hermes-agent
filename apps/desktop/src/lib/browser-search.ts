import type { FileSearchFrame } from '@hermes/shared/file-search'
import { fileSearchDecoder } from '@hermes/shared/file-search-stream'

import type { HermesFileSearchPayload } from '@/global'
import { requireBrowserConnection } from '@/lib/browser-connection'
import { type BrowserBootstrap, endpointUrl } from '@/lib/browser-transport'

/** Browser-hosted Desktop searches the server's filesystem with the same
 * same-origin cookie/session header as its existing file preview requests. */
export function createBrowserSearchBridge(
  bootstrap: BrowserBootstrap,
  bootstrapFor?: (connectionId?: null | string, sessionId?: string) => BrowserBootstrap
): Pick<Window['hermesDesktop'], 'searchFiles' | 'cancelFileSearch' | 'onFileSearchFrame'> {
  const listeners = new Set<(event: { id: string; frame: FileSearchFrame }) => void>()
  const active = new Map<string, AbortController>()

  return {
    onFileSearchFrame: listener => {
      listeners.add(listener)

      return () => {
        listeners.delete(listener)
      }
    },
    cancelFileSearch: async id => {
      active.get(id)?.abort()
    },
    searchFiles: async (payload: HermesFileSearchPayload) => {
      if (!bootstrapFor) {
        requireBrowserConnection(payload.connectionId)
      }

      const target = bootstrapFor?.(payload.connectionId, payload.sessionId) ?? bootstrap

      if (payload.native) {
        throw new Error('This window can search only the connected filesystem')
      }

      for (const controller of active.values()) {
        controller.abort()
      }

      const controller = new AbortController()
      active.set(payload.id, controller)

      const decoder = fileSearchDecoder(frame => {
        if (!controller.signal.aborted) {
          for (const listener of listeners) {
            listener({ id: payload.id, frame })
          }
        }
      })

      const timer = setTimeout(() => controller.abort(), (payload.request.timeoutMs ?? 15000) + 5000)

      try {
        const url = endpointUrl('/api/fs/search', target.basePath, payload.profile)

        if (payload.sessionId !== undefined) {
          url.searchParams.set('session_id', payload.sessionId)
        }

        const response = await fetch(url, {
          method: 'POST',
          credentials: 'same-origin',
          signal: controller.signal,
          headers: {
            'Content-Type': 'application/json',
            ...(target.token ? { 'X-Hermes-Session-Token': target.token } : {})
          },
          body: JSON.stringify(payload.request)
        })

        if (!response.ok) {
          throw new Error(
            `Search backend returned ${response.status}; update the connected backend if search is unavailable`
          )
        }

        if (!response.body || !response.headers.get('content-type')?.includes('application/x-ndjson')) {
          throw new Error('Search backend did not return a search stream')
        }

        const reader = response.body.getReader()

        try {
          while (true) {
            const { done, value } = await reader.read()

            if (done) {
              break
            }

            decoder.push(value)
          }

          decoder.finish()
        } finally {
          reader.releaseLock()
        }
      } finally {
        clearTimeout(timer)
        controller.abort()
        active.delete(payload.id)
      }
    }
  }
}
