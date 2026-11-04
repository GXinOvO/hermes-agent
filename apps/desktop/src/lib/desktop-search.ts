import { randomUuid } from '@hermes/shared'
import type { FileSearchFrame, FileSearchRequest } from '@hermes/shared/file-search'

import type { DesktopFsSessionScope } from '@/lib/desktop-fs'
import { captureFileSearchScope } from '@/lib/file-search-owner'

/** Capture one owner for the entire query; changing profiles cannot move a
 * running search to a different disk. No per-file renderer IO or local fallback. */
export async function searchDesktopFiles(
  request: FileSearchRequest,
  options: {
    signal: AbortSignal
    onFrame: (frame: FileSearchFrame) => void
    scope?: DesktopFsSessionScope
  }
): Promise<void> {
  const desktop = window.hermesDesktop

  if (!desktop?.searchFiles || !desktop.cancelFileSearch || !desktop.onFileSearchFrame) {
    throw new Error('Update and rebuild Hermes Desktop to enable native file search')
  }

  if (options.signal.aborted) {
    return
  }

  const id = randomUuid()
  const scope = options.scope?.owner ? options.scope : captureFileSearchScope(options.scope?.sessionId)
  const owner = scope.owner!
  let terminal = false

  const unsubscribe = desktop.onFileSearchFrame(event => {
    if (event.id === id && !options.signal.aborted) {
      terminal ||= event.frame.type === 'done' || event.frame.type === 'error'
      options.onFrame(event.frame)
    }
  })

  const onAbort = () => {
    void desktop.cancelFileSearch!(id).catch(() => {})
  }

  options.signal.addEventListener('abort', onAbort, { once: true })

  try {
    await desktop.searchFiles({ ...owner, sessionId: scope.sessionId || undefined, id, request })

    if (!terminal && !options.signal.aborted) {
      throw new Error('Search ended without a terminal result')
    }
  } finally {
    options.signal.removeEventListener('abort', onAbort)
    unsubscribe()
  }
}
