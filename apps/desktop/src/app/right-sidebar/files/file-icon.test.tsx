import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { fileKindForName, FileTreeIcon } from './file-icon'

afterEach(cleanup)

function iconOf(name: string) {
  const { container } = render(<FileTreeIcon name={name} />)
  const glyph = container.querySelector('svg, i')

  return { glyph, color: (glyph as HTMLElement | null)?.style.color ?? '' }
}

describe('FileTreeIcon', () => {
  it.each([
    ['C:\\project\\Main.PYI', 'python', 'tabler-icon-brand-python'],
    ['src/app.ts', 'typescript', 'tabler-icon-file-type-ts'],
    ['src/app.mjs', 'javascript', 'tabler-icon-brand-javascript'],
    ['src/view.tsx', 'react', 'tabler-icon-brand-react'],
    ['native/kernel.cpp', 'cpp', 'tabler-icon-brand-cpp'],
    ['native/runtime.h', 'c', 'tabler-icon-letter-c'],
    ['web/page.html', 'html', 'tabler-icon-file-type-html'],
    ['web/theme.scss', 'scss', 'tabler-icon-brand-sass'],
    ['server/main.go', 'go', 'tabler-icon-brand-golang'],
    ['server/lib.rs', 'rust', 'tabler-icon-file-type-rs'],
    ['README.md', 'markdown', 'codicon-markdown'],
    ['settings.json', 'json', 'codicon-json'],
    ['config.yaml', 'yaml', 'codicon-settings-gear'],
    ['launch.sh', 'shell', 'codicon-terminal'],
    ['scripts/run.ps1', 'powershell', 'codicon-terminal-powershell'],
    ['Dockerfile', 'docker', 'tabler-icon-brand-docker'],
    ['data/report.csv', 'csv', 'tabler-icon-file-type-csv'],
    ['docs/spec.pdf', 'pdf', 'tabler-icon-file-type-pdf'],
    ['assets/logo.svg', 'svg', 'tabler-icon-file-type-svg'],
    ['archive.tar.gz', 'archive', 'codicon-file-zip']
  ])('marks %s as %s', (name, kind, glyphClass) => {
    const { glyph, color } = iconOf(name)

    expect(fileKindForName(name)).toBe(kind)
    expect(glyph).not.toBeNull()
    expect(glyph?.getAttribute('class')).toContain(glyphClass)
    expect(color).toBe(`var(--fi-${kind})`)
    expect(glyph?.getAttribute('aria-hidden')).toBe('true')
  })

  it.each([
    ['package.json', 'npm', 'tabler-icon-brand-npm'],
    ['package-lock.json', 'npm', 'tabler-icon-brand-npm'],
    ['yarn.lock', 'npm', 'tabler-icon-brand-npm'],
    ['tsconfig.json', 'typescript', 'tabler-icon-file-type-ts'],
    ['vite.config.ts', 'vite', 'tabler-icon-brand-vite'],
    ['Dockerfile.dev', 'docker', 'tabler-icon-brand-docker'],
    ['.env.local', 'env', 'codicon-key'],
    ['.gitignore', 'git', 'tabler-icon-brand-git'],
    ['Makefile', 'make', 'codicon-tools'],
    ['CMakeLists.txt', 'make', 'codicon-tools'],
    ['LICENSE', 'license', 'codicon-law'],
    ['pyproject.toml', 'python', 'tabler-icon-brand-python'],
    ['requirements.txt', 'python', 'tabler-icon-brand-python']
  ])('prefers the exact filename rule for %s', (name, kind, glyphClass) => {
    const { glyph, color } = iconOf(name)

    expect(fileKindForName(name)).toBe(kind)
    expect(glyph?.getAttribute('class')).toContain(glyphClass)
    expect(color).toBe(`var(--fi-${kind})`)
  })

  it('changes the mark when a file is renamed to another type', () => {
    const { container, rerender } = render(<FileTreeIcon name="main.py" />)

    expect(container.querySelector('.tabler-icon-brand-python')).not.toBeNull()
    rerender(<FileTreeIcon name="main.ts" />)
    expect(container.querySelector('.tabler-icon-brand-python')).toBeNull()
    expect(container.querySelector('.tabler-icon-file-type-ts')).not.toBeNull()
  })

  it.each(['notes.unknown', 'py', 'json', '__proto__', 'constructor', 'file.toString', ''])(
    'keeps the neutral glyph for %s',
    name => {
      const { container } = render(<FileTreeIcon name={name} />)
      const glyph = container.querySelector('.codicon-file')

      expect(fileKindForName(name)).toBeUndefined()
      expect(glyph).not.toBeNull()
      expect((glyph as HTMLElement).style.color).toBe('')
    }
  )
})
