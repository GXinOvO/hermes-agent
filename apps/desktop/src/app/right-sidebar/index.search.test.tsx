import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { HermesReadDirResult, HermesReadFileTextResult } from '@/global'
import { I18nProvider } from '@/i18n'
import { $connection, $selectedStoredSessionId, $workspaceCwdOwner, setCurrentCwd } from '@/store/session'

import { resetProjectTreeState } from './files/use-project-tree'

import { RightSidebarPane } from './index'

/**
 * Wiring test for the Files pane: the header magnifier must switch the pane from the
 * tree to the search panel, and the panel must actually find something. The panel's
 * own behaviour is covered in files/search-panel.test.tsx; this covers the pane that
 * mounts it, which is where a missing/incorrect toggle would show up as "clicking
 * does nothing".
 */
const readDir = vi.fn<(path: string) => Promise<HermesReadDirResult>>()
const readFileText = vi.fn<(path: string) => Promise<HermesReadFileTextResult>>()

// jsdom has no layout engine: the tree measures its viewport and renders a skeleton
// instead of rows at 0×0, so give it a real box (same stub as files/tree.test.tsx).
const DOM_RECT = {
  bottom: 600,
  height: 600,
  left: 0,
  right: 240,
  toJSON: () => ({}),
  top: 0,
  width: 240,
  x: 0,
  y: 0
} as DOMRect

function installBridge() {
  ;(window as unknown as { hermesDesktop: unknown }).hermesDesktop = {
    git: { repoStatus: vi.fn(async () => null) },
    readDir,
    readFileText,
    readFileDataUrl: vi.fn(async () => 'data:text/plain;base64,')
  }
}

function renderPane() {
  return render(
    <I18nProvider configClient={null} initialLocale="en">
      <RightSidebarPane onActivateFile={vi.fn()} onActivateFolder={vi.fn()} />
    </I18nProvider>
  )
}

describe('Files pane search wiring', () => {
  beforeEach(() => {
    vi.stubGlobal('ResizeObserver', undefined)
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(() => DOM_RECT)

    $connection.set(null)
    $selectedStoredSessionId.set(null)
    $workspaceCwdOwner.set(null)
    resetProjectTreeState()

    readDir.mockReset()
    readDir.mockImplementation(async (path: string) => {
      if (path === '/repo') {
        return {
          entries: [
            { isDirectory: true, name: 'src', path: '/repo/src' },
            { isDirectory: false, name: 'README.md', path: '/repo/README.md' }
          ]
        }
      }

      if (path === '/repo/src') {
        return { entries: [{ isDirectory: false, name: 'app.py', path: '/repo/src/app.py' }] }
      }

      return { entries: [], error: 'ENOENT' }
    })

    readFileText.mockReset()
    readFileText.mockImplementation(async (path: string) => ({ path, text: 'TOKEN_NAME = 1\n' }))

    installBridge()
    setCurrentCwd('/repo')
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
    $connection.set(null)
    $selectedStoredSessionId.set(null)
    $workspaceCwdOwner.set(null)
    setCurrentCwd('')
    resetProjectTreeState()
    delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
  })

  it('swaps the tree for the search panel when the header magnifier is pressed', async () => {
    renderPane()

    // The tree is what the pane shows first.
    expect(await screen.findByText('README.md')).toBeTruthy()

    fireEvent.click(await screen.findByRole('button', { name: 'Search files' }))

    // The panel's own input replaces it.
    expect(await screen.findByLabelText('Search file names')).toBeTruthy()
    expect(screen.queryByText('README.md')).toBeNull()
  })

  it('finds a file in a folder the tree never expanded, from inside the pane', async () => {
    renderPane()

    fireEvent.click(await screen.findByRole('button', { name: 'Search files' }))
    fireEvent.change(await screen.findByLabelText('Search file names'), { target: { value: 'app.py' } })

    expect(await screen.findByRole('button', { name: 'app.py' }, { timeout: 3000 })).toBeTruthy()
  })

  it('goes back to the tree when the panel is closed', async () => {
    renderPane()

    fireEvent.click(await screen.findByRole('button', { name: 'Search files' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Close search' }))

    expect(await screen.findByText('README.md')).toBeTruthy()
    expect(screen.queryByLabelText('Search file names')).toBeNull()
  })
})
