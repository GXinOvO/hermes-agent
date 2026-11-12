import React, { useEffect, useState } from 'react'
import { Button, Codicon } from '@hermes/plugin-sdk'
import { readFollowupSummary } from './followup-notifications.mjs'

export function FollowupEntry({ host, t, notifications, storageName, readSummary = readFollowupSummary }) {
  const [count, setCount] = useState(null)
  const [failed, setFailed] = useState(false)
  useEffect(() => {
    let active = true, request = 0
    const refresh = async () => {
      const generation = ++request
      try {
        const value = await readSummary(globalThis.indexedDB, storageName)
        if (active && generation === request) { setCount(value.open); setFailed(false) }
      } catch {
        if (active && generation === request) { setCount(null); setFailed(true) }
      }
    }
    void refresh()
    const stop = notifications.subscribe(refresh)
    globalThis.window?.addEventListener('focus', refresh)
    return () => { active = false; stop(); globalThis.window?.removeEventListener('focus', refresh) }
  }, [notifications, storageName, readSummary])
  return <Button size="xs" variant="ghost" title={`${t('entryHint')}${failed ? ` · ${t('entryUnavailable')}` : ''}`} onClick={() => host.revealPane('task-overview:followup')}><Codicon name="checklist"/> {t('entry')} · {count === null ? '—' : count}</Button>
}
