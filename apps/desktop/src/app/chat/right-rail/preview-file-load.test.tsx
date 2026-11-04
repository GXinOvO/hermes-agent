import { Buffer } from 'node:buffer'

import { EditorView } from '@codemirror/view'
import { act, cleanup, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { HermesConnection, HermesReadFileTextResult } from '@/global'
import { $previewTabs, closeRightRail, openPreview, type PreviewTarget } from '@/store/preview'
import { $connection } from '@/store/session'

import { LocalFilePreview } from './preview-file'
import { readTextPreview, useLocalPreviewLoad } from './preview-file-load'

vi.mock('@/components/chat/shiki-highlighter', () => ({
  LazyShiki: ({ code }: { code: string }) => <pre>{code}</pre>
}))

const target: PreviewTarget = {
  kind: 'file',
  label: 'note.txt',
  path: '/workspace/note.txt',
  previewKind: 'text',
  source: '/workspace/note.txt',
  url: 'file:///workspace/note.txt'
}

const options = { forcePreview: false, reloadKey: 0, selfReload: 0, sessionId: 'first', target }
const rangeDescriptors = Object.getOwnPropertyDescriptors(Range.prototype)

beforeEach(() => {
  $connection.set({ connectionId: 'local', mode: 'local', profile: 'default' } as HermesConnection)
  Object.defineProperties(Range.prototype, {
    getClientRects: { configurable: true, value: () => [] },
    getBoundingClientRect: { configurable: true, value: () => new DOMRect() }
  })
})

afterEach(() => {
  cleanup()
  closeRightRail()
  $connection.set(null)
  vi.unstubAllGlobals()
  vi.restoreAllMocks()

  for (const key of ['getClientRects', 'getBoundingClientRect']) {
    if (rangeDescriptors[key]) {
      Object.defineProperty(Range.prototype, key, rangeDescriptors[key])
    } else {
      Reflect.deleteProperty(Range.prototype, key)
    }
  }
})

function pendingRead() {
  let resolve!: (value: HermesReadFileTextResult) => void

  const promise = new Promise<HermesReadFileTextResult>(release => {
    resolve = release
  })

  return { promise, resolve }
}

describe('local preview read ownership', () => {
  it.each(['file', 'connection', 'profile', 'session', 'reload', 'selfReload'] as const)(
    'ignores a superseded read after a %s change and waits for its current owner',
    async change => {
      const oldRead = pendingRead()
      const currentRead = pendingRead()
      const readFileText = vi.fn().mockReturnValueOnce(oldRead.promise).mockReturnValueOnce(currentRead.promise)
      vi.stubGlobal('hermesDesktop', { readFileText, gitRoot: async () => null })
      const rendered = renderHook(useLocalPreviewLoad, { initialProps: options })
      const next = { ...options }

      if (change === 'file') {
        next.target = { ...target, path: '/workspace/next.txt', url: 'file:///workspace/next.txt' }
      } else if (change === 'session') {
        next.sessionId = 'second'
      } else if (change === 'reload') {
        next.reloadKey += 1
      } else if (change === 'selfReload') {
        next.selfReload += 1
      }

      act(() => {
        if (change === 'connection' || change === 'profile') {
          $connection.set({
            ...$connection.get()!,
            ...(change === 'connection' ? { connectionId: 'other' } : { profile: 'other' })
          })
        }

        rendered.rerender(next)
      })
      expect(readFileText).toHaveBeenCalledTimes(2)
      await act(async () => oldRead.resolve({ path: target.path!, text: 'old owner' }))
      expect(rendered.result.current.state.loading).toBe(true)
      expect(rendered.result.current.navigationReady).toBe(false)
      await act(async () => currentRead.resolve({ path: next.target.path!, text: 'current owner' }))
      expect(rendered.result.current.state.text).toBe('current owner')
      expect(rendered.result.current.navigationReady).toBe(true)
    }
  )

  it('does not attach an old diff to identical text read for a new file', async () => {
    let releaseDiff!: (value: string) => void

    const oldDiff = new Promise<string>(resolve => {
      releaseDiff = resolve
    })

    const fileDiff = vi.fn().mockReturnValueOnce(oldDiff).mockRejectedValueOnce(new Error('not available'))
    vi.stubGlobal('hermesDesktop', {
      readFileText: async (path: string) => ({ path, text: 'same contents' }),
      gitRoot: async () => '/workspace',
      git: { fileDiff }
    })
    const rendered = renderHook(useLocalPreviewLoad, { initialProps: options })
    await waitFor(() => expect(fileDiff).toHaveBeenCalledTimes(1))
    rendered.rerender({
      ...options,
      target: { ...target, path: '/workspace/next.txt', url: 'file:///workspace/next.txt' }
    })
    await waitFor(() => expect(fileDiff).toHaveBeenCalledTimes(2))
    await act(async () => releaseDiff('diff from the previous file'))
    expect(rendered.result.current.state.text).toBe('same contents')
    expect(rendered.result.current.state.diff).toBeUndefined()
    expect(rendered.result.current.navigationReady).toBe(true)
  })

  it.each(['binary', 'large'] as const)('requires admission for a %s read result before showing text', async kind => {
    const readFileText = vi.fn(async () => ({
      path: target.path!,
      text: 'readable bytes',
      binary: kind === 'binary',
      byteSize: kind === 'large' ? 1024 * 1024 : 14
    }))

    const gitRoot = vi.fn(async () => null)
    vi.stubGlobal('hermesDesktop', { readFileText, gitRoot })
    const rendered = renderHook(useLocalPreviewLoad, { initialProps: options })
    await waitFor(() => expect(rendered.result.current.state.loading).toBe(false))
    expect(rendered.result.current.shouldBlock).toBe(true)
    expect(rendered.result.current.state.text).toBeUndefined()
    expect(gitRoot).not.toHaveBeenCalled()
    rendered.rerender({ ...options, forcePreview: true })
    await waitFor(() => expect(rendered.result.current.state.text).toBe('readable bytes'))
    expect(rendered.result.current.shouldBlock).toBe(false)
    expect(readFileText).toHaveBeenCalledTimes(2)
    expect(gitRoot).toHaveBeenCalledTimes(1)
  })

  it.each([true, false])('tombstones only confirmed absence (missing=%s)', async missing => {
    vi.stubGlobal('hermesDesktop', {
      readFileText: async () => {
        if (missing) {
          return { ok: false, error: 'ENOENT', message: 'file gone' }
        }

        throw new Error('temporarily unavailable')
      }
    })
    openPreview(target)
    const rendered = renderHook(useLocalPreviewLoad, { initialProps: options })
    await waitFor(() => expect(rendered.result.current.state.loading).toBe(false))
    expect(rendered.result.current.state.missing).toBe(missing)
    expect(rendered.result.current.state.error).toBe(missing ? 'file gone' : 'temporarily unavailable')
    expect(rendered.result.current.navigationReady).toBe(false)
    expect($previewTabs.get().find(tab => tab.target.url === target.url)?.target.missing ?? false).toBe(missing)
  })
})

describe('legacy text read compatibility', () => {
  it.each([
    ['base64 UTF-8', `data:text/plain;base64,${Buffer.from('中😀 text').toString('base64')}`, '中😀 text', false],
    ['percent encoded', 'data:text/plain,plain%20text%0A', 'plain text\n', false],
    ['binary bytes', 'data:application/octet-stream;base64,AAE=', '\u0000\u0001', true]
  ] as const)('decodes %s through the existing byte fallback', async (_kind, dataUrl, text, binary) => {
    const readFileDataUrl = vi.fn(async () => dataUrl)
    vi.stubGlobal('hermesDesktop', {
      readFileText: async () => {
        throw new Error("No handler registered for 'hermes:readFileText'")
      },
      readFileDataUrl
    })

    const result = await readTextPreview(target.path!)
    expect(result.text).toBe(text)
    expect(result.binary).toBe(binary)
    expect(readFileDataUrl).toHaveBeenCalledExactlyOnceWith(target.path)
  })

  it('does not route a transient text-read error through the legacy fallback', async () => {
    const readFileDataUrl = vi.fn()
    vi.stubGlobal('hermesDesktop', {
      readFileText: async () => {
        throw new Error('permission denied')
      },
      readFileDataUrl
    })
    await expect(readTextPreview(target.path!)).rejects.toThrow('permission denied')
    expect(readFileDataUrl).not.toHaveBeenCalled()
  })

  it('uses the same fallback for preview load and the pre-save conflict read', async () => {
    let contents = 'initial contents'
    const readFileDataUrl = vi.fn(async () => `data:text/plain;base64,${Buffer.from(contents).toString('base64')}`)

    const writeTextFile = vi.fn(async (path: string, text: string) => {
      contents = text

      return { path }
    })

    vi.stubGlobal('hermesDesktop', {
      readFileText: async () => {
        throw new Error("No handler registered for 'hermes:readFileText'")
      },
      readFileDataUrl,
      writeTextFile,
      gitRoot: async () => null
    })
    render(<LocalFilePreview reloadKey={0} target={target} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }))
    const view = EditorView.findFromDOM(screen.getByRole('textbox'))!
    act(() => view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: 'saved draft' } }))
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await screen.findByRole('button', { name: 'Edit' })
    expect(writeTextFile).toHaveBeenCalledExactlyOnceWith(target.path, 'saved draft')
    expect(readFileDataUrl).toHaveBeenCalledTimes(3)
    expect(contents).toBe('saved draft')
  })
})
