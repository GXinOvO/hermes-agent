import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'

import { EditorView } from '@codemirror/view'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { expect, it, vi } from 'vitest'

import { setApiRequestConnection } from '@/api/client'
import type { HermesApiRequest, HermesConnection } from '@/global'
import type { DesktopFsSessionScope } from '@/lib/desktop-fs'
import type { PreviewTarget } from '@/store/preview'
import { $connection } from '@/store/session'

import { LocalFilePreview } from './preview-file'

vi.mock('@/components/chat/shiki-highlighter', () => ({ LazyShiki: ({ code }: { code: string }) => <pre>{code}</pre> }))

function activate(mode: 'local' | 'remote', connectionId: string) {
  $connection.set({ mode, connectionId, profile: 'ambient' } as HermesConnection)
  setApiRequestConnection(connectionId)
}

it.each(['connection', 'profile', 'session', 'native', 'target'] as const)(
  'saves only the captured %s file after a pending validation crosses an ambient switch', async kind => {
    const directory = await mkdtemp(path.join(tmpdir(), 'hermes-captured-save-'))
    const ownedFile = path.join(directory, 'owned.txt')
    const otherFile = path.join(directory, 'other.txt')
    const logical = kind === 'native' ? 'C:\\project\\note.txt' : '/workspace/note.txt'

    const scope: DesktopFsSessionScope = {
      owner: { native: kind === 'native', connectionId: 'file-host', profile: 'file-profile' }, sessionId: 'file-session'
    }

    const writes: string[] = []
    const requests: HermesApiRequest[] = []
    let reads = 0
    let release!: () => void
    let started!: () => void
    const pending = new Promise<void>(resolve => { release = resolve })
    const validationStarted = new Promise<void>(resolve => { started = resolve })
    const rangeDescriptors = Object.getOwnPropertyDescriptors(Range.prototype)

    const read = async (file: string) => {
      const text = await readFile(file, 'utf8')

      if (file === ownedFile && ++reads === 2) { started(); await pending }

      return { path: logical, text, truncated: false, binary: false }
    }

    const write = async (file: string, content: string) => {
      writes.push(file)
      await writeFile(file, content)

      return { path: logical }
    }

    try {
      await writeFile(ownedFile, 'original source')
      await writeFile(otherFile, 'other valuable source')
      vi.stubGlobal('hermesDesktop', {
        api: async (request: HermesApiRequest) => {
          requests.push(request)
          const owned = request.connectionId === 'file-host' && request.profile === 'file-profile'
          const actual = owned ? ownedFile : otherFile

          if (request.path.startsWith('/api/fs/read-text?')) {
            const query = new URL(request.path, 'http://test').searchParams
            expect(query.get('session_id')).toBe(owned ? 'file-session' : 'new-file-session')

            return read(actual)
          }

          if (request.path === '/api/fs/write-text') {
            expect((request.body as { session_id: string }).session_id).toBe('file-session')

            return write(actual, (request.body as { content: string }).content)
          }

          throw new Error('Unexpected ambient enrichment request: ' + request.path)
        },
        readFileText: async () => read(kind === 'native' ? ownedFile : otherFile),
        writeTextFile: async (_path: string, content: string) => write(kind === 'native' ? ownedFile : otherFile, content),
        gitRoot: vi.fn(() => { throw new Error('Captured file must not use ambient git') })
      })
      Object.defineProperties(Range.prototype, {
        getClientRects: { configurable: true, value: () => [] },
        getBoundingClientRect: { configurable: true, value: () => new DOMRect() }
      })
      activate(kind === 'native' ? 'remote' : 'local', 'ambient-before')

      const target: PreviewTarget = { kind: 'file', label: 'note.txt', path: logical, source: logical,
        url: kind === 'native' ? 'file:///C:/project/note.txt' : 'file:///workspace/note.txt', previewKind: 'text', fileScope: scope }

      const rendered = render(<LocalFilePreview reloadKey={0} sessionId="ambient-session" target={target} />)
      fireEvent.click(await screen.findByRole('button', { name: 'Edit' }))
      const editor = EditorView.findFromDOM(rendered.container.querySelector('.cm-content')!)!
      act(() => editor.dispatch({ changes: { from: 0, to: editor.state.doc.length, insert: 'captured draft' } }))
      fireEvent.click(screen.getByRole('button', { name: 'Save' }))
      await validationStarted
      await act(async () => {
        const next = { mode: kind === 'native' ? 'local' : 'remote', connectionId: 'ambient-after', profile: kind === 'profile' ? 'other-profile' : 'ambient' } as HermesConnection
        $connection.set(next)
        setApiRequestConnection(next.connectionId || null)

        const nextTarget = kind === 'target'
          ? { ...target, fileScope: { owner: { ...scope.owner!, connectionId: 'new-file-host' }, sessionId: 'new-file-session' } }
          : target

        rendered.rerender(<LocalFilePreview reloadKey={0} sessionId={kind === 'session' ? 'other-session' : 'ambient-session'} target={nextTarget} />)
        release()
      })
      await screen.findByRole('button', { name: 'Edit' })
      expect(writes).toEqual(kind === 'target' ? [] : [ownedFile])
      expect(await readFile(ownedFile, 'utf8')).toBe(kind === 'target' ? 'original source' : 'captured draft')
      expect(await readFile(otherFile, 'utf8')).toBe('other valuable source')

      if (kind === 'native') {expect(requests).toEqual([])}
      else if (kind !== 'target') {expect(requests.every(request => request.connectionId === 'file-host' && request.profile === 'file-profile')).toBe(true)}
    } finally {
      release?.()
      cleanup()
      vi.unstubAllGlobals()
      $connection.set(null)
      setApiRequestConnection(null)

      for (const key of ['getClientRects', 'getBoundingClientRect']) {
        const descriptor = rangeDescriptors[key]

        if (descriptor) {Object.defineProperty(Range.prototype, key, descriptor)}
        else {Reflect.deleteProperty(Range.prototype, key)}
      }

      await rm(directory, { recursive: true, force: true })
    }
  }
)
