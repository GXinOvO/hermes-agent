import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { $rightRailActiveTabId } from './layout'
import { $previewTabs, $previewTarget, closeRightRail, openPreview, type PreviewTarget } from './preview'
import { $activeSessionId, $selectedStoredSessionId } from './session'

const target: PreviewTarget = {
  kind: 'file',
  label: 'note.md',
  language: 'markdown',
  path: '/workspace/note.md',
  previewKind: 'text',
  source: '/workspace/note.md',
  url: 'file:///workspace/note.md'
}

beforeEach(() => {
  $activeSessionId.set(null)
  $selectedStoredSessionId.set(null)
  closeRightRail()
  window.localStorage.clear()
})

afterEach(() => {
  closeRightRail()
  window.localStorage.clear()
})

describe('preview navigation requests', () => {
  it('gives repeated identical locations fresh requests on the same focused file tab', () => {
    const request = { ...target, location: { line: 8, column: 4 } }
    openPreview(request)
    const first = $previewTarget.get()!
    const id = $rightRailActiveTabId.get()
    expect(first.renderMode).toBe('source')
    expect(first.location).toEqual({ line: 8, column: 4 })
    expect(first.locationRequestId).toEqual(expect.any(Number))

    openPreview(request)
    const second = $previewTarget.get()!
    expect(second.locationRequestId).not.toBe(first.locationRequestId)
    expect($rightRailActiveTabId.get()).toBe(id)
    expect($previewTabs.get()).toHaveLength(1)

    openPreview({ ...target, location: { line: 12, column: 2 } })
    expect($previewTarget.get()?.location).toEqual({ line: 12, column: 2 })
    expect($previewTabs.get()).toHaveLength(1)
  })

  it('does not replay an old location from persisted tabs', () => {
    $selectedStoredSessionId.set('navigation-session')
    openPreview({ ...target, location: { line: 12, column: 2 } })
    const stored = JSON.parse(window.localStorage.getItem('hermes.desktop.previewTabs.v2')!)
    const restored = Object.values(stored).flat() as Array<{ target: PreviewTarget }>
    expect(restored).toHaveLength(1)
    expect(restored[0].target.location).toBeUndefined()
    expect(restored[0].target.locationRequestId).toBeUndefined()
    expect(restored[0].target.renderMode).toBe('source')
  })
})
