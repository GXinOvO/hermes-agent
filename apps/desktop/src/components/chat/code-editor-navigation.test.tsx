import { EditorState } from '@codemirror/state'
import { EditorView } from '@codemirror/view'
import { act, cleanup, render } from '@testing-library/react'
import { createRef } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { CodeEditor, type CodeEditorApi } from './code-editor'

const descriptors = Object.getOwnPropertyDescriptors(Range.prototype)

beforeEach(() => {
  Object.defineProperties(Range.prototype, {
    getClientRects: { configurable: true, value: () => [] },
    getBoundingClientRect: { configurable: true, value: () => new DOMRect() }
  })
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()

  for (const key of ['getClientRects', 'getBoundingClientRect']) {
    if (descriptors[key]) {
      Object.defineProperty(Range.prototype, key, descriptors[key])
    } else {
      Reflect.deleteProperty(Range.prototype, key)
    }
  }
})

describe('CodeEditor source navigation', () => {
  it.each([
    [{ line: 2, column: 4 }, 2, 3],
    [{ line: 2 }, 2, 0],
    [{ line: 0, column: -20 }, 1, 0],
    [{ line: 200, column: 200 }, 4, 4],
    [{ line: 3, column: 300 }, 3, 0],
    [{ line: 2.9, column: 4.9 }, 2, 3],
    [{ line: Number.NaN, column: Number.NaN }, 1, 0]
  ] as const)('clamps %j using CodeMirror CRLF lines and UTF-16 offsets', (location, line, offset) => {
    const rendered = render(
      <CodeEditor
        filePath="/workspace/note.txt"
        initialValue={'first\r\n中😀MATCH\r\n\r\nlast'}
        location={location}
        locationRequestId={1}
        onChange={vi.fn()}
        readOnly
      />
    )

    const view = EditorView.findFromDOM(rendered.getByRole('textbox'))!
    expect(view.state.doc.lines).toBe(4)
    expect(view.state.selection.main.head).toBe(view.state.doc.line(line).from + offset)
    expect(view.state.facet(EditorState.readOnly)).toBe(true)
  })

  it('uses the public API for repeated explicit jumps, while source is read-only and selectable', () => {
    const apiRef = createRef<CodeEditorApi>()
    const changed = vi.fn()
    const scroll = vi.spyOn(EditorView, 'scrollIntoView')

    const rendered = render(
      <CodeEditor
        apiRef={apiRef}
        filePath="/workspace/note.txt"
        initialValue={'one\ntwo\nthree'}
        onChange={changed}
        readOnly
      />
    )

    const textbox = rendered.getByRole('textbox')
    const view = EditorView.findFromDOM(textbox)!
    expect(textbox.getAttribute('tabindex')).toBe('0')
    expect(textbox.getAttribute('aria-readonly')).toBe('true')
    expect(textbox.getAttribute('contenteditable')).toBe('false')

    act(() => apiRef.current!.revealLocation({ line: 3, column: 3 }))
    expect(view.state.selection.main.head).toBe(view.state.doc.line(3).from + 2)
    act(() => view.dispatch({ selection: { anchor: 0 } }))
    act(() => apiRef.current!.revealLocation({ line: 3, column: 3 }))
    expect(view.state.selection.main.head).toBe(view.state.doc.line(3).from + 2)
    expect(scroll).toHaveBeenCalledTimes(2)
    expect(changed).not.toHaveBeenCalled()
    expect(rendered.container.querySelector('.cm-hermes-search-line')?.textContent).toBe('three')
  })
})
