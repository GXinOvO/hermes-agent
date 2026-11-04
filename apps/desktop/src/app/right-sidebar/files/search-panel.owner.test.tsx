import type { FileSearchFrame, FileSearchPayload } from '@hermes/shared/file-search'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { setApiRequestConnection } from '@/api/client'
import { $layoutTree } from '@/components/pane-shell/tree/store'
import { captureFileSearchScope } from '@/lib/file-search-owner'
import { $connection, $selectedStoredSessionId, setSessions } from '@/store/session'
import { $sessionTiles } from '@/store/session-states'

import { FileSearchPanel } from './search-panel'

interface Run {
  payload: FileSearchPayload
  emit: (frame: FileSearchFrame) => void
  finish: () => void
}

function bridge() {
  let listener: (event: { id: string; frame: FileSearchFrame }) => void = () => {}
  const runs: Run[] = []
  const cancelFileSearch = vi.fn(async () => {})
  vi.stubGlobal('hermesDesktop', {
    api: vi.fn(async () => ({ entries: [] })), cancelFileSearch,
    onFileSearchFrame: (callback: typeof listener) => {
      listener = callback

      return () => {}
    },
    searchFiles: vi.fn((payload: FileSearchPayload) => new Promise<void>(finish => {
      const captured = listener
      runs.push({ payload, finish, emit: frame => captured({ id: payload.id, frame }) })
    }))
  })

  return { runs, cancelFileSearch }
}

function session(id: string, profile = 'docker') {
  $selectedStoredSessionId.set(id)
  setSessions([{ id, profile, cwd: '/workspace', connection_id: 'host' } as never])
  $sessionTiles.set([{
    storedSessionId: id, runtimeId: `${id}-runtime`, workspaceMode: 'sessions',
    ownerRoute: { connectionId: 'host', profile, mode: 'remote' }
  } as never])
}

const done: FileSearchFrame = { type: 'done', truncated: false, warnings: [], elapsedMs: 1 }
const hit = (name: string): FileSearchFrame => ({ type: 'batch', hits: [{ path: `/workspace/${name}`, rel: name }] })

afterEach(() => {
  cleanup()
  $sessionTiles.set([])
  $selectedStoredSessionId.set(null)
  setSessions([])
  $layoutTree.set(null)
  $connection.set(null)
  setApiRequestConnection(null)
  vi.unstubAllGlobals()
})

it('refuses an unresolved focused owner visibly without starting ambient search', async () => {
  const h = bridge()
  $selectedStoredSessionId.set('unknown-session')
  $connection.set({ mode: 'remote', profile: 'ambient' } as never)
  render(<FileSearchPanel onClose={() => {}} onOpenFile={() => {}} root="/workspace" />)
  fireEvent.change(screen.getByLabelText('Search file names'), { target: { value: 'needle' } })
  expect(await screen.findByText(/filesystem owner is unknown/)).toBeTruthy()
  expect(h.runs).toHaveLength(0)
  expect(screen.queryByText('No matches')).toBeNull()
})

it('cancels a same-root same-profile search when its session changes and ignores its late batch', async () => {
  const h = bridge()
  session('one')
  render(<FileSearchPanel onClose={() => {}} onOpenFile={() => {}} root="/workspace" />)
  fireEvent.change(screen.getByLabelText('Search file names'), { target: { value: 'needle' } })
  await waitFor(() => expect(h.runs).toHaveLength(1))
  await act(async () => { h.runs[0].emit(hit('one.py')); session('two') })
  await waitFor(() => expect(h.runs).toHaveLength(2))
  expect(h.cancelFileSearch).toHaveBeenCalledWith(h.runs[0].payload.id)
  await act(async () => {
    h.runs[0].emit(hit('late.py')); h.runs[0].emit(done); h.runs[0].finish()
    h.runs[1].emit(hit('two.py')); h.runs[1].emit(done); h.runs[1].finish()
  })
  expect(await screen.findByRole('button', { name: 'two.py' })).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'late.py' })).toBeNull()
  expect(screen.queryByRole('button', { name: 'one.py' })).toBeNull()
  expect(h.runs[1].payload.sessionId).toBe('two')
})

it('opens a result with the root and exact filesystem context captured by that run', async () => {
  const h = bridge()
  session('one')
  const onOpenFile = vi.fn()
  render(<FileSearchPanel onClose={() => {}} onOpenFile={onOpenFile} root="/workspace" />)
  fireEvent.change(screen.getByLabelText('Search file names'), { target: { value: 'needle' } })
  await waitFor(() => expect(h.runs).toHaveLength(1))
  await act(async () => { h.runs[0].emit(hit('one.py')); h.runs[0].emit(done); h.runs[0].finish() })
  fireEvent.click(await screen.findByRole('button', { name: 'one.py' }))
  expect(onOpenFile).toHaveBeenCalledWith('/workspace/one.py', undefined, {
    root: '/workspace', sessionId: 'one', owner: { native: false, connectionId: 'host', profile: 'docker' }
  })
})

it('cancels query and profile replacements before accepting the new owner results', async () => {
  const h = bridge()
  session('one')
  render(<FileSearchPanel onClose={() => {}} onOpenFile={() => {}} root="/workspace" />)
  fireEvent.change(screen.getByLabelText('Search file names'), { target: { value: 'old' } })
  await waitFor(() => expect(h.runs).toHaveLength(1))
  fireEvent.change(screen.getByLabelText('Search file names'), { target: { value: 'new' } })
  await waitFor(() => expect(h.runs).toHaveLength(2))
  expect(h.cancelFileSearch).toHaveBeenCalledWith(h.runs[0].payload.id)
  await act(async () => { session('one', 'another-profile') })
  expect(captureFileSearchScope('one').owner?.profile).toBe('another-profile')
  await waitFor(() => expect(h.runs).toHaveLength(3))
  expect(h.cancelFileSearch).toHaveBeenCalledWith(h.runs[1].payload.id)
  expect(h.runs[2].payload).toMatchObject({ profile: 'another-profile', sessionId: 'one', request: { query: 'new' } })
  await act(async () => { session('one', 'another-profile') })
  expect(h.cancelFileSearch).toHaveBeenCalledTimes(2)
  await act(async () => {
    for (const run of h.runs.slice(0, 2)) { run.emit(hit('late.py')); run.emit(done); run.finish() }
    h.runs[2].emit(hit('current.py')); h.runs[2].emit(done); h.runs[2].finish()
  })
  expect(await screen.findByRole('button', { name: 'current.py' })).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'late.py' })).toBeNull()
})
