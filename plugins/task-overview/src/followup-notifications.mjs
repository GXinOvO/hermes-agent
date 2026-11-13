import { openFollowupStore } from './followup-indexeddb.mjs'

export const FOLLOWUP_CHANGED = 'hermes.task-overview.followup.changed'
export const FOLLOWUP_CHANNEL = 'hermes.task-overview.followup.v1'

export function createFollowupNotifications(target = globalThis.window, Channel = globalThis.BroadcastChannel) {
  let channel = null
  try { if (typeof Channel === 'function') channel = new Channel(FOLLOWUP_CHANNEL) } catch {}
  const callbacks = new Set()
  const signal = source => { for (const callback of callbacks) { try { callback(source) } catch {} } }
  const external = () => signal(undefined)
  target?.addEventListener?.(FOLLOWUP_CHANGED, external)
  if (channel) channel.onmessage = external
  return {
    subscribe(callback) { callbacks.add(callback); return () => callbacks.delete(callback) },
    publish(source) {
      signal(source)
      try { channel?.postMessage('changed') } catch {}
    },
    close() {
      callbacks.clear()
      target?.removeEventListener?.(FOLLOWUP_CHANGED, external)
      channel?.close()
    }
  }
}

export async function readFollowupSummary(factory = globalThis.indexedDB, name) {
  const store = await openFollowupStore(factory, name)
  try {
    const rows = await store.list()
    return { open: rows.filter(row => row.task.resolution.state === 'open').length }
  } finally { store.close() }
}
