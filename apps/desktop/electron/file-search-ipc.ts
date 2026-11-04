import type { FileSearchFrame, FileSearchRequest } from '@hermes/shared/file-search'
import { validateFileSearchRequest } from '@hermes/shared/file-search'

interface Sender {
  id: number
  isDestroyed: () => boolean
  send: (channel: string, payload: unknown) => void
  once: (name: string, listener: () => void) => unknown
  removeListener: (name: string, listener: () => void) => unknown
}

interface SearchRun {
  signal: AbortSignal
  onFrame: (frame: FileSearchFrame) => void
}

interface SearchOwner {
  connectionId?: string
  profile?: string
  sessionId?: string
}

function searchOwner(raw: Record<string, unknown>): SearchOwner {
  const owner: SearchOwner = {}

  for (const name of ['connectionId', 'profile', 'sessionId'] as const) {
    const value = raw[name]

    if (value === undefined) {
      continue
    }

    if (typeof value !== 'string' || value.length > 4096 || value.includes('\0') || (name === 'sessionId' && !value.trim())) {
      throw new Error('Invalid search owner')
    }

    if (value) {
      owner[name] = value
    }
  }

  return owner
}

interface FileSearchIpcDeps {
  ipcMain: { handle: (name: string, handler: (event: any, ...args: any[]) => unknown) => void }
  runNative: (request: FileSearchRequest, run: SearchRun) => Promise<void>
  runRemote: (owner: SearchOwner, request: FileSearchRequest, run: SearchRun) => Promise<void>
}

/** One search per window. Replacing it, closing its window or cancelling aborts the
 * same child/request; a sibling window cannot cancel it by guessing its request id. */
export function registerFileSearchIpc({ ipcMain, runNative, runRemote }: FileSearchIpcDeps) {
  const active = new Map<number, { id: string; controller: AbortController }>()
  ipcMain.handle('hermes:fs:search:cancel', (event, id) => {
    const run = active.get(event.sender.id)

    if (run && run.id === id) {
      run.controller.abort()
    }
  })
  ipcMain.handle('hermes:fs:search', async ({ sender }, raw) => {
    const id = typeof raw?.id === 'string' ? raw.id : ''

    if (!id || id.length > 128 || typeof raw?.native !== 'boolean') {
      throw new Error('Invalid search request')
    }

    const request = validateFileSearchRequest(raw.request)
    const owner = searchOwner(raw)

    active.get(sender.id)?.controller.abort()

    const controller = new AbortController()
    const slot = { id, controller }
    const onDestroyed = () => controller.abort()

    const run: SearchRun = {
      signal: controller.signal,
      onFrame: frame => {
        if (active.get(sender.id) === slot && !controller.signal.aborted && !sender.isDestroyed()) {
          sender.send('hermes:fs:search:frame', { id, frame })
        }
      }
    }

    active.set(sender.id, slot)
    sender.once('destroyed', onDestroyed)

    try {
      if (raw.native) {
        await runNative(request, run)
      } else {
        await runRemote(owner, request, run)
      }
    } finally {
      sender.removeListener('destroyed', onDestroyed)

      if (active.get(sender.id) === slot) {
        active.delete(sender.id)
      }
    }
  })
}
