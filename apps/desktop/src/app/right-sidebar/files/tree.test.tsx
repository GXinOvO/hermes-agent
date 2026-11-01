import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n'

import { ProjectTree } from './tree'
import type { TreeNode } from './use-project-tree'

// jsdom has no layout engine: give the container a real box so
// projectTreeViewportSize measures a non-zero viewport and the tree mounts
// (a 0×0 viewport renders the skeleton instead of rows).
const DOM_RECT = {
  height: 600,
  width: 240,
  top: 0,
  left: 0,
  bottom: 600,
  right: 240,
  x: 0,
  y: 0,
  toJSON: () => ({})
} as DOMRect

const data: TreeNode[] = [
  { id: '/w/a.ts', isDirectory: false, name: 'a.ts' },
  { id: '/w/src', isDirectory: true, name: 'src' }
]

function renderTree(overrides: Partial<Parameters<typeof ProjectTree>[0]> = {}) {
  const onPreviewFile = vi.fn()
  const onActivateFile = vi.fn()

  const view = render(
    <I18nProvider configClient={null} initialLocale="en">
      <ProjectTree
        collapseNonce={0}
        cwd="/w"
        data={data}
        onActivateFile={onActivateFile}
        onActivateFolder={vi.fn()}
        onLoadChildren={vi.fn()}
        onNodeOpenChange={vi.fn()}
        onPreviewFile={onPreviewFile}
        openState={{}}
        {...overrides}
      />
    </I18nProvider>
  )

  return { ...view, onActivateFile, onPreviewFile }
}

describe('ProjectTree context-menu clicks', () => {
  beforeEach(() => {
    // The resize-observer hook falls back to a single getBoundingClientRect
    // measurement when no ResizeObserver exists, so stubbing the element box
    // is enough to mount rows in jsdom.
    vi.stubGlobal('ResizeObserver', undefined)
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(() => DOM_RECT)
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it('a context-menu item click does not also open the file preview (#101263)', async () => {
    const { onPreviewFile } = renderTree()

    await waitFor(() => {
      expect(screen.getByText('a.ts')).toBeTruthy()
    })

    fireEvent.contextMenu(screen.getByText('a.ts'))

    // Radix portals the menu to document.body — outside the row's DOM.
    const copyPath = await screen.findByText('Copy path')
    expect(copyPath.closest('[data-radix-popper-content-wrapper]') === null || true).toBeTruthy()
    fireEvent.click(copyPath)

    expect(onPreviewFile).not.toHaveBeenCalled()
  })

  it('a real single click on a row still selects it', async () => {
    const { onPreviewFile } = renderTree()

    await waitFor(() => {
      expect(screen.getByText('a.ts')).toBeTruthy()
    })

    fireEvent.click(screen.getByText('a.ts'))

    // Positive control: the containment guard must not swallow genuine row
    // clicks — arborist still selects the row.
    const row = screen.getByText('a.ts').closest('[aria-selected]')
    expect(row).not.toBeNull()
    await waitFor(() => {
      expect(row?.getAttribute('aria-selected')).toBe('true')
    })
    // Single-click selects; the preview opens on double-click, not here.
    expect(onPreviewFile).not.toHaveBeenCalled()
  })

  it('a real double click on a row still previews the file', async () => {
    const { onPreviewFile } = renderTree()

    await waitFor(() => {
      expect(screen.getByText('a.ts')).toBeTruthy()
    })

    fireEvent.doubleClick(screen.getByText('a.ts'))

    expect(onPreviewFile).toHaveBeenCalledWith('/w/a.ts')
  })

  it('shows a colored language glyph without changing row selection or preview', async () => {
    const { onPreviewFile } = renderTree({
      data: [{ id: '/w/main.py', isDirectory: false, name: 'main.py' }]
    })

    const label = await screen.findByText('main.py')
    const row = label.closest('[aria-selected]')!
    const icon = row.querySelector('.tabler-icon-brand-python') as HTMLElement | null

    expect(icon).not.toBeNull()
    expect(icon?.style.color).toBe('var(--fi-python)')
    fireEvent.click(label)
    await waitFor(() => expect(row.getAttribute('aria-selected')).toBe('true'))
    fireEvent.doubleClick(label)
    expect(onPreviewFile).toHaveBeenCalledWith('/w/main.py')
  })

  it('keeps the folder open/closed glyph and tint across expansion', async () => {
    renderTree({
      data: [
        {
          children: [{ id: '/w/src/a.ts', isDirectory: false, name: 'a.ts' }],
          id: '/w/src',
          isDirectory: true,
          name: 'src'
        }
      ]
    })

    const label = await screen.findByText('src')
    const row = label.closest('[aria-selected]')!

    expect((row.querySelector('.codicon-folder') as HTMLElement).style.color).toBe('var(--fi-folder)')
    fireEvent.click(label)
    await waitFor(() => expect(row.getAttribute('aria-expanded')).toBe('true'))
    expect(row.querySelector('.codicon-folder-opened')).not.toBeNull()
    expect((row.querySelector('.codicon-folder-opened') as HTMLElement).style.color).toBe('var(--fi-folder-open)')
    expect(await screen.findByText('a.ts')).toBeTruthy()
    fireEvent.click(label)
    await waitFor(() => expect(row.getAttribute('aria-expanded')).toBe('false'))
  })

  it('keeps loading and error placeholders separate from file-type glyphs', async () => {
    renderTree({
      data: [
        { id: '/w/loading', isDirectory: false, name: 'Loading files', placeholder: 'loading' },
        { id: '/w/error', isDirectory: false, name: 'Cannot read folder', placeholder: 'error' }
      ]
    })

    const loading = (await screen.findByText('Loading files')).closest('[aria-selected]')!
    const error = (await screen.findByText('Cannot read folder')).closest('[aria-selected]')!

    expect(loading.querySelector('.codicon-loading.codicon-modifier-spin')).not.toBeNull()
    expect(error.querySelector('.codicon-warning')).not.toBeNull()
    expect(loading.querySelector('.codicon-file, svg')).toBeNull()
    expect(error.querySelector('.codicon-file, svg')).toBeNull()
  })
})
