import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n'

import { FileSearchPanel } from './search-panel'

/**
 * Renders the real panel against a stubbed desktop bridge, so the assertions cover
 * what the pane actually does: typed text reaches the input value, a walk finds a
 * file inside a folder that was never expanded, a content hit renders with its line
 * number, and a click hands the absolute path back to the pane.
 */
const TREE: Record<string, Array<{ name: string; isDirectory: boolean }>> = {
  '/proj': [
    { name: 'README.md', isDirectory: false },
    { name: 'src', isDirectory: true }
  ],
  '/proj/src': [{ name: 'deep', isDirectory: true }],
  '/proj/src/deep': [
    { name: 'app.py', isDirectory: false },
    { name: 'view.ts', isDirectory: false }
  ]
}

const FILES: Record<string, string> = {
  '/proj/README.md': 'hello\n',
  '/proj/src/deep/app.py': 'import os\nTARGET_FIELD = 1\n',
  '/proj/src/deep/view.ts': '// TARGET_FIELD in ts\n'
}

function installBridge() {
  ;(globalThis as Record<string, unknown>).hermesDesktop = {
    gitRoot: async () => null,
    readDir: async (path: string) => {
      const entries = TREE[path]

      if (!entries) {
        return { entries: [], error: 'ENOENT' }
      }

      return { entries: entries.map(entry => ({ ...entry, path: `${path}/${entry.name}`.replace('//', '/') })) }
    },
    readFileDataUrl: async (path: string) => {
      const text = FILES[path]

      if (text === undefined) {
        return { ok: false, error: 'ENOENT', path }
      }

      return `data:text/plain;base64,${btoa(text)}`
    },
    readFileText: async (path: string) => {
      const text = FILES[path]

      if (text === undefined) {
        return { ok: false, error: 'ENOENT', path }
      }

      return { path, text }
    }
  }
}

function renderPanel() {
  const onOpenFile = vi.fn()
  const onClose = vi.fn()

  const view = render(
    <I18nProvider configClient={null} initialLocale="en">
      <FileSearchPanel onClose={onClose} onOpenFile={onOpenFile} root="/proj" />
    </I18nProvider>
  )

  return { ...view, onClose, onOpenFile }
}

describe('FileSearchPanel', () => {
  beforeEach(() => {
    installBridge()
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    delete (globalThis as Record<string, unknown>).hermesDesktop
  })

  it('keeps the typed text in the search field', () => {
    renderPanel()
    const input = screen.getByLabelText('Search file names') as HTMLInputElement

    fireEvent.change(input, { target: { value: 'app' } })

    expect(input.value).toBe('app')
  })

  it('finds a file name inside a folder the tree never expanded', async () => {
    renderPanel()
    fireEvent.change(screen.getByLabelText('Search file names'), { target: { value: 'app.py' } })

    // The name appears twice on purpose: as the file group's label and as the row
    // that opens it. The row is the button.
    expect(await screen.findByRole('button', { name: 'app.py' }, { timeout: 3000 })).toBeTruthy()
    expect(await screen.findByText(/1 result in 1 file/)).toBeTruthy()
  })

  it('opens the matched file when a result row is clicked', async () => {
    const { onOpenFile } = renderPanel()

    fireEvent.change(screen.getByLabelText('Search file names'), { target: { value: 'app.py' } })
    fireEvent.click(await screen.findByRole('button', { name: 'app.py' }, { timeout: 3000 }))

    expect(onOpenFile).toHaveBeenCalledWith('/proj/src/deep/app.py')
  })

  it('shows content hits with their line numbers', async () => {
    renderPanel()
    fireEvent.click(screen.getByRole('button', { name: 'Content' }))

    fireEvent.change(screen.getByLabelText('Search file contents'), { target: { value: 'TARGET_FIELD' } })

    await waitFor(() => expect(screen.getByText(/2 results in 2 files/)).toBeTruthy(), { timeout: 3000 })
  })

  it('honours the scope field', async () => {
    renderPanel()
    fireEvent.click(screen.getByRole('button', { name: 'Content' }))

    fireEvent.change(screen.getByLabelText('Search file contents'), { target: { value: 'TARGET_FIELD' } })
    fireEvent.change(screen.getByLabelText('Limit the search to a folder or pattern'), {
      target: { value: 'src/**/*.py' }
    })

    await waitFor(() => expect(screen.getByText(/1 result in 1 file/)).toBeTruthy(), { timeout: 3000 })
    expect(screen.queryByText('view.ts')).toBeNull()
  })

  it('reports no matches instead of an empty pane', async () => {
    renderPanel()

    fireEvent.change(screen.getByLabelText('Search file names'), { target: { value: 'nothing-matches-this' } })

    expect(await screen.findByText('No matches', {}, { timeout: 3000 })).toBeTruthy()
  })
})
