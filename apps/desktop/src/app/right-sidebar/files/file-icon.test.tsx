import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { FileTreeIcon } from './file-icon'

afterEach(cleanup)

describe('FileTreeIcon', () => {
  it.each([
    ['C:\\project\\Main.PYI', 'svg.tabler-icon-brand-python'],
    ['src/app.ts', 'svg.tabler-icon-brand-typescript'],
    ['src/app.mjs', 'svg.tabler-icon-brand-javascript'],
    ['src/view.tsx', 'svg.tabler-icon-brand-react'],
    ['native/kernel.cpp', 'svg.tabler-icon-brand-cpp'],
    ['native/runtime.h', 'svg.tabler-icon-letter-c'],
    ['README.md', '.codicon-markdown'],
    ['settings.json', '.codicon-json'],
    ['config.yaml', '.codicon-settings-gear'],
    ['launch.sh', '.codicon-terminal'],
    ['Dockerfile', 'svg.tabler-icon-brand-docker']
  ])('renders a recognizable type glyph for %s', (name, selector) => {
    const { container } = render(<FileTreeIcon name={name} />)
    const icon = container.querySelector(selector)

    expect(icon).not.toBeNull()
    expect(icon?.getAttribute('aria-hidden')).toBe('true')
    expect(icon?.getAttribute('class')).toMatch(
      /text-\(--(?:ui|file-icon)-(blue|cyan|yellow|green|orange|red|purple)\)/
    )
  })

  it('gives package.json a package glyph instead of the generic JSON glyph', () => {
    const { container } = render(<FileTreeIcon name="C:\\work\\PACKAGE.JSON" />)

    expect(container.querySelector('svg.tabler-icon-brand-npm')).not.toBeNull()
    expect(container.querySelector('.codicon-json')).toBeNull()
  })

  it.each([
    ['Dockerfile.dev', 'svg.tabler-icon-brand-docker'],
    ['package-lock.json', 'svg.tabler-icon-brand-npm'],
    ['.env.local', '.codicon-settings-gear'],
    ['Makefile', '.codicon-settings-gear'],
    ['CMakeLists.txt', '.codicon-settings-gear'],
    ['.gitignore', '.codicon-settings-gear'],
    ['.zshrc', '.codicon-terminal']
  ])('recognizes the configuration filename %s', (name, selector) => {
    const { container } = render(<FileTreeIcon name={name} />)

    expect(container.querySelector(selector)).not.toBeNull()
  })

  it('changes the glyph after a file is renamed to a different type', () => {
    const { container, rerender } = render(<FileTreeIcon name="main.py" />)

    expect(container.querySelector('svg.tabler-icon-brand-python')).not.toBeNull()
    rerender(<FileTreeIcon name="main.ts" />)
    expect(container.querySelector('svg.tabler-icon-brand-python')).toBeNull()
    expect(container.querySelector('svg.tabler-icon-brand-typescript')).not.toBeNull()
  })

  it.each(['notes.unknown', 'LICENSE', 'py', 'json', '__proto__', 'constructor', 'file.toString'])(
    'keeps the generic glyph for %s',
    name => {
      const { container } = render(<FileTreeIcon name={name} />)

      expect(container.querySelector('.codicon-file')).not.toBeNull()
      expect(container.firstElementChild?.getAttribute('class')).not.toMatch(
        /text-\(--(?:ui|file-icon)-(blue|cyan|yellow|green|orange|red|purple)\)/
      )
    }
  )
})
