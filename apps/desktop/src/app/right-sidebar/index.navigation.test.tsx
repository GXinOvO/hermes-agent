import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import * as localPreview from '@/lib/local-preview'
import { $previewTabs, $previewTarget, closeRightRail } from '@/store/preview'
import { $connection, $selectedStoredSessionId, $workspaceCwdOwner, setCurrentCwd } from '@/store/session'

import { resetProjectTreeState } from './files/use-project-tree'

import { RightSidebarPane } from './index'

// The search producer is parent-owned. This seam exercises its agreed one-based
// callback through the real pane and preview store, without duplicating its engine.
vi.mock('./files/search-panel', () => ({
  FileSearchPanel: ({
    onOpenFile
  }: {
    onOpenFile: (path: string, location?: { line: number; column?: number }) => void
  }) => (
    <>
      <button onClick={() => onOpenFile('/repo/note.md', { line: 3, column: 4 })} type="button">
        Content result
      </button>
      <button onClick={() => onOpenFile('/repo/note.md')} type="button">
        Filename result
      </button>
    </>
  )
}))

beforeEach(() => {
  $connection.set(null)
  $selectedStoredSessionId.set(null)
  $workspaceCwdOwner.set(null)
  resetProjectTreeState()
  closeRightRail()
  vi.stubGlobal('hermesDesktop', {
    git: { repoStatus: async () => null },
    readDir: async () => ({ entries: [] })
  })
  setCurrentCwd('/repo')
})

afterEach(() => {
  cleanup()
  closeRightRail()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  $connection.set(null)
  $selectedStoredSessionId.set(null)
  $workspaceCwdOwner.set(null)
  setCurrentCwd('')
  resetProjectTreeState()
})

it('carries a content location through the pane and reuses its tab for repeat clicks', async () => {
  render(<RightSidebarPane onActivateFile={vi.fn()} onActivateFolder={vi.fn()} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Search files' }))
  fireEvent.click(screen.getByRole('button', { name: 'Content result' }))

  await waitFor(() => expect($previewTarget.get()?.location).toEqual({ line: 3, column: 4 }))
  const first = $previewTarget.get()?.locationRequestId
  fireEvent.click(screen.getByRole('button', { name: 'Content result' }))
  await waitFor(() => expect($previewTarget.get()?.locationRequestId).not.toBe(first))
  expect($previewTabs.get()).toHaveLength(1)

  fireEvent.click(screen.getByRole('button', { name: 'Filename result' }))
  await waitFor(() => expect($previewTarget.get()?.location).toBeUndefined())
  expect($previewTabs.get()).toHaveLength(1)
})

it('keeps the last click when file normalization resolves out of order', async () => {
  let first!: (target: ReturnType<typeof localPreview.localPreviewTarget>) => void

  const normalize = vi
    .spyOn(localPreview, 'normalizeOrLocalPreviewTarget')
    .mockImplementationOnce(
      () =>
        new Promise(resolve => {
          first = resolve
        })
    )
    .mockResolvedValueOnce(localPreview.localPreviewTarget('/repo/note.md'))

  render(<RightSidebarPane onActivateFile={vi.fn()} onActivateFolder={vi.fn()} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Search files' }))
  fireEvent.click(screen.getByRole('button', { name: 'Content result' }))
  fireEvent.click(screen.getByRole('button', { name: 'Filename result' }))
  await waitFor(() => expect($previewTarget.get()?.path).toBe('/repo/note.md'))
  await act(async () => first(localPreview.localPreviewTarget('/repo/note.md')))
  expect($previewTarget.get()?.location).toBeUndefined()
  expect(normalize).toHaveBeenCalledTimes(2)
})
