import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'

import { EditorView } from '@codemirror/view'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { setApiRequestConnection } from '@/api/client'
import type { HermesApiRequest, HermesConnection } from '@/global'
import type { PreviewTarget } from '@/store/preview'
import { $connection } from '@/store/session'

import { LocalFilePreview } from './preview-file'

vi.mock('@/components/chat/shiki-highlighter', () => ({ LazyShiki: ({ code }: { code: string }) => <pre>{code}</pre> }))
const scratch = tmpdir()
const ranges = Object.getOwnPropertyDescriptors(Range.prototype)

function deferred() {
  let release!: () => void

  const promise = new Promise<void>(resolve => {
    release = resolve
  })

  return { promise, release }
}

function fileTarget(host: string): PreviewTarget {
  return {
    kind: 'file',
    label: 'note.txt',
    path: '/workspace/note.txt',
    source: '/workspace/note.txt',
    url: 'file:///workspace/note.txt',
    previewKind: 'text',
    fileScope: { owner: { native: false, connectionId: host, profile: 'profile' }, sessionId: host + '-session' }
  }
}

function editor(container: HTMLElement) {
  const node = container.querySelector<HTMLElement>('.cm-content')

  return node ? EditorView.findFromDOM(node)! : null
}

function type(container: HTMLElement, value: string) {
  const view = editor(container)!
  act(() => view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: value } }))
}

async function harness(pause: 'validation' | 'write' | 'write-error') {
  const dir = await mkdtemp(path.join(scratch, 'review-v2-save-'))
  const a = path.join(dir, 'owner-a.txt')
  const b = path.join(dir, 'owner-b.txt')
  await writeFile(a, 'original source')
  await writeFile(b, 'other owner source')
  const entered = deferred()
  const gate = deferred()
  const finished = deferred()
  const validationReturned = deferred()
  const writes: Array<{ host: string; content: string }> = []
  let reads = 0
  vi.stubGlobal('hermesDesktop', {
    api: async (request: HermesApiRequest) => {
      const host = request.connectionId!
      const actual = host === 'owner-a' ? a : b

      if (request.path.startsWith('/api/fs/read-text?')) {
        const text = await readFile(actual, 'utf8')

        if (host === 'owner-a' && ++reads === 2 && pause === 'validation') {
          entered.release()
          await gate.promise
          validationReturned.release()
        }

        return { text, truncated: false, binary: false }
      }

      if (request.path === '/api/fs/write-text') {
        const body = request.body as { content: string; session_id: string }
        expect(body.session_id).toBe(host + '-session')
        writes.push({ host, content: body.content })

        if (pause !== 'validation') {
          entered.release()
          await gate.promise
        }

        if (pause === 'write-error') {
          finished.release()
          throw new Error('old write rejected')
        }

        await writeFile(actual, body.content)
        finished.release()

        return { path: '/workspace/note.txt' }
      }

      throw new Error('Unexpected request: ' + request.path)
    }
  })
  Object.defineProperties(Range.prototype, {
    getClientRects: { configurable: true, value: () => [] },
    getBoundingClientRect: { configurable: true, value: () => new DOMRect() }
  })
  $connection.set({ mode: 'local', connectionId: 'ambient' } as HermesConnection)
  setApiRequestConnection('ambient')
  const rendered = render(<LocalFilePreview reloadKey={0} target={fileTarget('owner-a')} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Edit' }))
  type(rendered.container, 'first explicitly saved draft')
  fireEvent.click(screen.getByRole('button', { name: 'Save' }))
  await entered.promise

  return { ...rendered, a, b, dir, gate, finished, validationReturned, writes }
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  $connection.set(null)
  setApiRequestConnection(null)

  for (const key of ['getClientRects', 'getBoundingClientRect']) {
    if (ranges[key]) {
      Object.defineProperty(Range.prototype, key, ranges[key])
    } else {
      Reflect.deleteProperty(Range.prototype, key)
    }
  }
})

it('a cancelled validation must not save a replacement edit on the same captured file', async () => {
  const h = await harness('validation')

  try {
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }))
    type(h.container, 'replacement draft never submitted')
    await act(async () => {
      h.gate.release()
      await h.validationReturned.promise
    })
    // The retired operation must finish its read without starting a write.
    expect(editor(h.container)?.state.doc.toString()).toBe('replacement draft never submitted')
    expect(h.writes).toEqual([])
  } finally {
    h.gate.release()
    cleanup()
    await rm(h.dir, { recursive: true, force: true })
  }
})

it('old owner write completion must not close a new captured owner draft', async () => {
  const h = await harness('write')

  try {
    h.rerender(<LocalFilePreview reloadKey={0} target={fileTarget('owner-b')} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }))
    type(h.container, 'new owner draft never submitted')
    await act(async () => {
      h.gate.release()
      await h.finished.promise
    })
    await waitFor(() =>
      expect(h.container.querySelector('.cm-content') || screen.queryByRole('button', { name: 'Edit' })).not.toBeNull()
    )
    const observed = editor(h.container)?.state.doc.toString() ?? null
    console.log(
      'REVIEW_OBSERVATION',
      JSON.stringify({
        scenario: 'owner-write-completion',
        writes: h.writes,
        owned: await readFile(h.a, 'utf8'),
        other: await readFile(h.b, 'utf8'),
        visibleDraft: observed,
        editButtonVisible: !!screen.queryByRole('button', { name: 'Edit' })
      })
    )
    expect(observed).toBe('new owner draft never submitted')
  } finally {
    h.gate.release()
    cleanup()
    await rm(h.dir, { recursive: true, force: true })
  }
})

it('an old failed write must not show errors or clear the next editor lifetime', async () => {
  const h = await harness('write-error')

  try {
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }))
    type(h.container, 'new unsaved edit after cancellation')
    await act(async () => {
      h.gate.release()
      await h.finished.promise
    })
    expect(editor(h.container)?.state.doc.toString()).toBe('new unsaved edit after cancellation')
    expect(screen.queryByText(/old write rejected/)).toBeNull()
    expect(screen.getByRole('button', { name: 'Save' }).hasAttribute('disabled')).toBe(false)
    expect(await readFile(h.a, 'utf8')).toBe('original source')
  } finally {
    h.gate.release()
    cleanup()
    await rm(h.dir, { recursive: true, force: true })
  }
})

it('typing while a captured write is pending must retain the later unsaved draft', async () => {
  const h = await harness('write')

  try {
    type(h.container, 'later draft typed during save')
    await act(async () => {
      h.gate.release()
      await h.finished.promise
    })
    await waitFor(() =>
      expect(h.container.querySelector('.cm-content') || screen.queryByRole('button', { name: 'Edit' })).not.toBeNull()
    )
    const observed = editor(h.container)?.state.doc.toString() ?? null
    console.log(
      'REVIEW_OBSERVATION',
      JSON.stringify({
        scenario: 'typing-during-write',
        writes: h.writes,
        owned: await readFile(h.a, 'utf8'),
        other: await readFile(h.b, 'utf8'),
        visibleDraft: observed
      })
    )
    expect(observed).toBe('later draft typed during save')
    expect(await readFile(h.a, 'utf8')).toBe('first explicitly saved draft')
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await screen.findByRole('button', { name: 'Edit' })
    expect(await readFile(h.a, 'utf8')).toBe('later draft typed during save')
    expect(h.writes.map(write => write.content)).toEqual([
      'first explicitly saved draft',
      'later draft typed during save'
    ])
  } finally {
    h.gate.release()
    cleanup()
    await rm(h.dir, { recursive: true, force: true })
  }
})
