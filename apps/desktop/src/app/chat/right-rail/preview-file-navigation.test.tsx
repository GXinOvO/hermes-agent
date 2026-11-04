import { EditorState } from '@codemirror/state'
import { EditorView } from '@codemirror/view'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { setApiRequestConnection, setApiRequestProfile } from '@/api/client'
import type { PreviewTarget } from '@/store/preview'
import { $connection } from '@/store/session'

import { LocalFilePreview } from './preview-file'
import { PreviewPane } from './preview-pane'

vi.mock(import('@/components/chat/shiki-highlighter'), async importOriginal => ({
  ...(await importOriginal()),
  LazyShiki: ({ code }: { code: string }) => <pre>{code}</pre>
}))
vi.mock('./real-profile-consent-dialog', () => ({ RealProfileConsentDialog: () => null }))

const target: PreviewTarget = {
  kind: 'file',
  label: 'note.md',
  language: 'markdown',
  path: '/workspace/note.md',
  previewKind: 'text',
  source: '/workspace/note.md',
  url: 'file:///workspace/note.md'
}

const rangeDescriptors = Object.getOwnPropertyDescriptors(Range.prototype)

beforeEach(() => {
  Object.defineProperties(Range.prototype, {
    getClientRects: { configurable: true, value: () => [] },
    getBoundingClientRect: { configurable: true, value: () => new DOMRect() }
  })
})

afterEach(() => {
  cleanup()
  $connection.set(null)
  setApiRequestConnection(null)
  setApiRequestProfile(null)
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

describe('search-result file navigation', () => {
  it('waits for the read, opens markdown source read-only and lands at a one-based UTF-16 column', async () => {
    let release!: (value: { path: string; text: string }) => void

    const readFileText = vi.fn(
      () =>
        new Promise<{ path: string; text: string }>(resolve => {
          release = resolve
        })
    )

    vi.stubGlobal('hermesDesktop', { readFileText, gitRoot: async () => null })

    const rendered = render(
      <LocalFilePreview reloadKey={0} target={{ ...target, location: { line: 3, column: 4 }, locationRequestId: 1 }} />
    )

    expect(screen.queryByRole('textbox')).toBeNull()

    await act(async () => release({ path: target.path!, text: '# Header\r\n\r\n中😀MATCH tail\r\nlast' }))

    const textbox = await screen.findByRole('textbox')
    const view = EditorView.findFromDOM(textbox)!
    const line = view.state.doc.line(3)
    expect(view.state.selection.main.head).toBe(line.from + 3)
    expect(view.state.facet(EditorState.readOnly)).toBe(true)
    expect(view.contentDOM.getAttribute('contenteditable')).toBe('false')
    expect(rendered.container.querySelector('[data-preview-markdown]')).toBeNull()
    expect(rendered.container.querySelector('.cm-hermes-search-line')?.textContent).toContain('MATCH tail')
  })

  it('repeats the same jump in the same editor without re-reading or forcing subsequent cursor/scroll changes', async () => {
    const text = Array.from({ length: 2000 }, (_, n) => `line ${n + 1}`).join('\n')
    const readFileText = vi.fn(async () => ({ path: target.path!, text }))
    vi.stubGlobal('hermesDesktop', { readFileText, gitRoot: async () => null })
    const location = { line: 1600, column: 6 }
    const scrollRequest = vi.spyOn(EditorView, 'scrollIntoView')
    const rendered = render(<LocalFilePreview reloadKey={0} target={{ ...target, location, locationRequestId: 1 }} />)
    const view = EditorView.findFromDOM(await screen.findByRole('textbox'))!
    expect(view.state.selection.main.head).toBe(view.state.doc.line(1600).from + 5)
    const requests = scrollRequest.mock.calls.length

    act(() => view.dispatch({ selection: { anchor: 1 } }))
    view.scrollDOM.scrollTop = 90
    fireEvent.scroll(view.scrollDOM)
    rendered.rerender(
      <LocalFilePreview
        reloadKey={0}
        target={{ ...target, label: 'updated', location: { ...location }, locationRequestId: 1 }}
      />
    )
    expect(view.state.selection.main.head).toBe(1)
    expect(view.scrollDOM.scrollTop).toBe(90)
    expect(scrollRequest).toHaveBeenCalledTimes(requests)

    rendered.rerender(<LocalFilePreview reloadKey={0} target={{ ...target, location, locationRequestId: 2 }} />)
    expect(EditorView.findFromDOM(screen.getByRole('textbox'))).toBe(view)
    expect(view.state.selection.main.head).toBe(view.state.doc.line(1600).from + 5)
    expect(scrollRequest).toHaveBeenCalledTimes(requests + 1)
    expect(readFileText).toHaveBeenCalledTimes(1)

    rendered.rerender(
      <LocalFilePreview reloadKey={0} target={{ ...target, location: { line: 10, column: 2 }, locationRequestId: 3 }} />
    )
    expect(view.state.selection.main.head).toBe(view.state.doc.line(10).from + 1)
    expect(readFileText).toHaveBeenCalledTimes(1)
  })

  it('keeps a new navigation in source after the user previously selected rendered markdown', async () => {
    vi.stubGlobal('hermesDesktop', {
      readFileText: async () => ({ path: target.path!, text: '# Heading\nsecond\nthird' }),
      gitRoot: async () => null
    })

    const rendered = render(
      <LocalFilePreview reloadKey={0} target={{ ...target, location: { line: 2 }, locationRequestId: 1 }} />
    )

    await screen.findByRole('textbox')
    fireEvent.click(screen.getByRole('button', { name: 'PREVIEW' }))
    expect(rendered.container.querySelector('[data-preview-markdown]')).not.toBeNull()

    rendered.rerender(
      <LocalFilePreview reloadKey={0} target={{ ...target, location: { line: 3 }, locationRequestId: 2 }} />
    )
    const view = EditorView.findFromDOM(await screen.findByRole('textbox'))!
    expect(view.state.selection.main.head).toBe(view.state.doc.line(3).from)
    expect(rendered.container.querySelector('[data-preview-markdown]')).toBeNull()
  })

  it('leaves the diff view for source on a fresh request and does not replay it when switching modes later', async () => {
    vi.stubGlobal('hermesDesktop', {
      readFileText: async () => ({ path: target.path!, text: '# Heading\nmatch\nlast' }),
      gitRoot: async () => '/workspace',
      git: {
        fileDiff: async () => 'diff --git a/note.md b/note.md\n--- a/note.md\n+++ b/note.md\n@@ -1 +1 @@\n-old\n+new\n'
      }
    })

    const rendered = render(<LocalFilePreview reloadKey={0} target={target} />)
    const diff = await screen.findByRole('button', { name: 'DIFF' })
    await waitFor(() => expect(diff.className).toContain('text-foreground'))
    rendered.rerender(
      <LocalFilePreview reloadKey={0} target={{ ...target, location: { line: 2, column: 2 }, locationRequestId: 1 }} />
    )
    const view = EditorView.findFromDOM(await screen.findByRole('textbox'))!
    expect(view.state.selection.main.head).toBe(view.state.doc.line(2).from + 1)

    fireEvent.click(screen.getByRole('button', { name: 'DIFF' }))
    expect(screen.queryByRole('textbox')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'SOURCE' }))
    const returned = EditorView.findFromDOM(await screen.findByRole('textbox'))!
    expect(returned.state.selection.main.head).toBe(0)
    expect(rendered.container.querySelector('.cm-hermes-search-line')).toBeNull()
  })

  it('retains navigation until the user admits a large-file preview', async () => {
    const readFileText = vi.fn(async () => ({ path: target.path!, text: '# Heading\nmatch\nlast', truncated: true }))
    vi.stubGlobal('hermesDesktop', { readFileText, gitRoot: async () => null })
    render(
      <LocalFilePreview
        reloadKey={0}
        target={{ ...target, large: true, location: { line: 2, column: 2 }, locationRequestId: 1 }}
      />
    )
    expect(readFileText).not.toHaveBeenCalled()
    fireEvent.click(await screen.findByRole('button', { name: 'Preview anyway' }))
    const view = EditorView.findFromDOM(await screen.findByRole('textbox'))!
    expect(view.state.selection.main.head).toBe(view.state.doc.line(2).from + 1)
    expect(readFileText).toHaveBeenCalledTimes(1)
  })

  it('navigates the live editable buffer without losing unsaved edits', async () => {
    const readFileText = vi.fn(async () => ({ path: target.path!, text: 'one\ntwo\nthree' }))
    vi.stubGlobal('hermesDesktop', { readFileText, gitRoot: async () => null })
    const rendered = render(<LocalFilePreview reloadKey={0} target={target} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }))
    const view = EditorView.findFromDOM(screen.getByRole('textbox'))!
    act(() => view.dispatch({ changes: { from: 0, insert: 'unsaved ' } }))

    rendered.rerender(
      <LocalFilePreview reloadKey={0} target={{ ...target, location: { line: 2, column: 2 }, locationRequestId: 1 }} />
    )
    expect(EditorView.findFromDOM(screen.getByRole('textbox'))).toBe(view)
    expect(view.state.doc.toString()).toBe('unsaved one\ntwo\nthree')
    expect(view.state.selection.main.head).toBe(view.state.doc.line(2).from + 1)
    expect(view.state.readOnly).toBe(false)
    expect((screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement).disabled).toBe(false)
    expect(readFileText).toHaveBeenCalledTimes(1)
  })

  it('reports an absent requested line in a truncated preview without falsely highlighting its last line', async () => {
    vi.stubGlobal('hermesDesktop', {
      readFileText: async () => ({ path: target.path!, text: 'one\ntwo', truncated: true }),
      gitRoot: async () => null
    })

    const rendered = render(
      <LocalFilePreview
        reloadKey={0}
        target={{ ...target, location: { line: 900, column: 4 }, locationRequestId: 1 }}
      />
    )

    const status = await screen.findByRole('status', { name: 'Preview unavailable' })
    expect(status.textContent).toContain('900:4')
    expect(status.textContent).toContain('unavailable')
    expect(status.textContent).toContain('Showing first 512 KB.')
    expect(rendered.container.querySelector('.cm-hermes-search-line')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Edit' })).toBeNull()
  })

  it('clears an old highlight when a repeated request is outside the truncated text and still permits rendered mode', async () => {
    vi.stubGlobal('hermesDesktop', {
      readFileText: async () => ({ path: target.path!, text: '# one\ntwo', truncated: true }),
      gitRoot: async () => null
    })

    const rendered = render(
      <LocalFilePreview reloadKey={0} target={{ ...target, location: { line: 2 }, locationRequestId: 1 }} />
    )

    await screen.findByRole('textbox')
    expect(rendered.container.querySelector('.cm-hermes-search-line')).not.toBeNull()
    rendered.rerender(
      <LocalFilePreview reloadKey={0} target={{ ...target, location: { line: 900 }, locationRequestId: 2 }} />
    )
    await screen.findByRole('status', { name: 'Preview unavailable' })
    expect(rendered.container.querySelector('.cm-hermes-search-line')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'PREVIEW' }))
    expect(rendered.container.querySelector('[data-preview-markdown]')).not.toBeNull()
  })

  it('rejects a column outside the truncated final line instead of highlighting its loaded end', async () => {
    vi.stubGlobal('hermesDesktop', {
      readFileText: async () => ({ path: target.path!, text: 'first\n0123456789', truncated: true }),
      gitRoot: async () => null
    })

    const rendered = render(
      <LocalFilePreview
        reloadKey={0}
        target={{ ...target, location: { line: 2, column: 1000 }, locationRequestId: 1 }}
      />
    )

    await screen.findByRole('textbox')
    const status = screen.queryByRole('status', { name: 'Preview unavailable' })
    expect(status).not.toBeNull()
    expect(status?.textContent).toContain('2:1000')
    expect(rendered.container.querySelector('.cm-hermes-search-line')).toBeNull()
  })

  it('clears the previous highlight for an unavailable UTF-16 column and still permits rendered mode', async () => {
    vi.stubGlobal('hermesDesktop', {
      readFileText: async () => ({ path: target.path!, text: '# first\r\n中😀x', truncated: true }),
      gitRoot: async () => null
    })

    const rendered = render(
      <LocalFilePreview reloadKey={0} target={{ ...target, location: { line: 2, column: 4 }, locationRequestId: 1 }} />
    )

    const view = EditorView.findFromDOM(await screen.findByRole('textbox'))!
    expect(view.state.selection.main.head).toBe(view.state.doc.line(2).from + 3)
    expect(rendered.container.querySelector('.cm-hermes-search-line')).not.toBeNull()
    rendered.rerender(
      <LocalFilePreview reloadKey={0} target={{ ...target, location: { line: 2, column: 6 }, locationRequestId: 2 }} />
    )
    expect(screen.queryByRole('status', { name: 'Preview unavailable' })).not.toBeNull()
    expect(EditorView.findFromDOM(screen.getByRole('textbox'))).toBe(view)
    expect(view.state.selection.main.head).toBe(view.state.doc.line(2).from + 3)
    expect(rendered.container.querySelector('.cm-hermes-search-line')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'PREVIEW' }))
    expect(rendered.container.querySelector('[data-preview-markdown]')).not.toBeNull()
    expect(screen.queryByRole('status', { name: 'Preview unavailable' })).not.toBeNull()
  })

  it.each([
    ['an untruncated final line', false, 2, 1000],
    ['an earlier complete line', true, 1, 1000],
    ['the loaded UTF-16 end position', true, 2, 5]
  ] as const)('preserves clamping and highlighting for %s', async (_label, truncated, line, column) => {
    vi.stubGlobal('hermesDesktop', {
      readFileText: async () => ({ path: target.path!, text: 'first\n中😀x', truncated }),
      gitRoot: async () => null
    })

    const rendered = render(
      <LocalFilePreview reloadKey={0} target={{ ...target, location: { line, column }, locationRequestId: 1 }} />
    )

    const view = EditorView.findFromDOM(await screen.findByRole('textbox'))!
    expect(view.state.selection.main.head).toBe(view.state.doc.line(line).to)
    expect(screen.queryByRole('status', { name: 'Preview unavailable' })).toBeNull()
    expect(rendered.container.querySelector('.cm-hermes-search-line')).not.toBeNull()
  })

  it('uses the newest navigation while the same file is still loading', async () => {
    let release!: (value: { path: string; text: string }) => void

    const readFileText = vi.fn(
      () =>
        new Promise<{ path: string; text: string }>(resolve => {
          release = resolve
        })
    )

    vi.stubGlobal('hermesDesktop', { readFileText, gitRoot: async () => null })

    const rendered = render(
      <LocalFilePreview reloadKey={0} target={{ ...target, location: { line: 2 }, locationRequestId: 1 }} />
    )

    rendered.rerender(
      <LocalFilePreview reloadKey={0} target={{ ...target, location: { line: 4, column: 3 }, locationRequestId: 2 }} />
    )
    await act(async () => release({ path: target.path!, text: 'one\ntwo\nthree\nfour' }))
    const view = EditorView.findFromDOM(await screen.findByRole('textbox'))!
    expect(view.state.selection.main.head).toBe(view.state.doc.line(4).from + 2)
    expect(readFileText).toHaveBeenCalledTimes(1)
  })

  it('waits for the new file rather than consuming its navigation against the previous file text', async () => {
    let release!: (value: { path: string; text: string }) => void

    const readFileText = vi
      .fn()
      .mockResolvedValueOnce({ path: target.path!, text: '# old\nold contents' })
      .mockImplementationOnce(
        () =>
          new Promise<{ path: string; text: string }>(resolve => {
            release = resolve
          })
      )

    vi.stubGlobal('hermesDesktop', { readFileText, gitRoot: async () => null })
    const rendered = render(<LocalFilePreview reloadKey={0} target={target} />)
    await screen.findByRole('button', { name: 'Edit' })

    const nextTarget = {
      ...target,
      path: '/workspace/next.md',
      url: 'file:///workspace/next.md',
      location: { line: 4, column: 3 },
      locationRequestId: 1
    }

    rendered.rerender(<LocalFilePreview reloadKey={0} target={nextTarget} />)
    await act(async () => release({ path: nextTarget.path, text: '# next\nsecond\nthird\nfourth' }))
    const view = EditorView.findFromDOM(await screen.findByRole('textbox'))!
    expect(view.state.doc.toString()).toContain('# next')
    expect(view.state.selection.main.head).toBe(view.state.doc.line(4).from + 2)
  })

  it('reveals source through the HTML pane without creating a rendered guest', async () => {
    vi.stubGlobal('hermesDesktop', {
      readFileText: async () => ({ path: '/workspace/note.html', text: '<h1>Hello</h1>\n<p>match</p>' }),
      gitRoot: async () => null
    })

    const rendered = render(
      <PreviewPane
        target={{
          ...target,
          path: '/workspace/note.html',
          previewKind: 'html',
          renderMode: 'source',
          location: { line: 2, column: 4 },
          locationRequestId: 1
        }}
      />
    )

    const view = EditorView.findFromDOM(await screen.findByRole('textbox'))!
    expect(view.state.selection.main.head).toBe(view.state.doc.line(2).from + 3)
    expect(rendered.container.querySelector('webview, iframe')).toBeNull()
  })

  it('navigates SSH-backed text at the existing connection/profile API seam', async () => {
    $connection.set({ connectionId: 'ssh-backend', mode: 'remote', profile: 'default' } as never)
    setApiRequestConnection('ssh-backend')
    setApiRequestProfile('default')

    const api = vi.fn(async ({ path }: { path: string }) =>
      path.startsWith('/api/fs/read-text?') ? { path: target.path!, text: '# remote\n中😀match\nlast' } : { root: null }
    )

    const readFileText = vi.fn()
    vi.stubGlobal('hermesDesktop', { api, readFileText })
    render(
      <LocalFilePreview
        reloadKey={0}
        sessionId="ssh-session"
        target={{ ...target, location: { line: 2, column: 4 }, locationRequestId: 1 }}
      />
    )
    const view = EditorView.findFromDOM(await screen.findByRole('textbox'))!
    expect(view.state.selection.main.head).toBe(view.state.doc.line(2).from + 3)
    expect(api).toHaveBeenCalledWith(
      expect.objectContaining({
        path: expect.stringContaining('/api/fs/read-text?path='),
        connectionId: 'ssh-backend',
        profile: 'default'
      })
    )
    expect(readFileText).not.toHaveBeenCalled()
  })
})
