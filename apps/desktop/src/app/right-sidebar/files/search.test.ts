import { mkdir, mkdtemp, readdir, readFile, rm, stat, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'

import { afterAll, beforeAll, describe, expect, it } from 'vitest'

import {
  compileQuery,
  type FileSearchIo,
  findLineMatches,
  globToRegExp,
  groupHitsByFile,
  matchesScope,
  relativeToRoot,
  runFileSearch
} from './search'

const fsIo: FileSearchIo = {
  readDir: async dir => {
    const entries = await readdir(dir, { withFileTypes: true })

    return { entries: entries.map(e => ({ isDirectory: e.isDirectory(), name: e.name, path: path.join(dir, e.name) })) }
  },
  readText: async file => {
    const size = (await stat(file)).size

    return { text: await readFile(file, 'utf8'), truncated: size > 4096 }
  }
}

describe('glob scope', () => {
  it('matches a bare extension at any depth', () => {
    const re = globToRegExp('*.ts')!

    expect(re.test('a.ts')).toBe(true)
    expect(re.test('src/deep/a.ts')).toBe(true)
    expect(re.test('a.tsx')).toBe(false)
  })

  it('lets ** span folders and zero folders', () => {
    const re = globToRegExp('src/**/*.py')!

    expect(re.test('src/a.py')).toBe(true)
    expect(re.test('src/x/y/a.py')).toBe(true)
    expect(re.test('other/a.py')).toBe(false)
    expect(re.test('src/a.ts')).toBe(false)
  })

  it('expands brace alternation inside one folder', () => {
    const re = globToRegExp('src/*.{ts,tsx}')!

    expect(re.test('src/a.ts')).toBe(true)
    expect(re.test('src/a.tsx')).toBe(true)
    expect(re.test('src/a.js')).toBe(false)
    expect(re.test('src/deep/a.ts')).toBe(false)
  })
})

describe('scope filter', () => {
  it('treats a plain folder as a prefix on a path boundary', () => {
    expect(matchesScope('src/a.ts', 'src')).toBe(true)
    expect(matchesScope('src/a.ts', 'src/')).toBe(true)
    expect(matchesScope('src2/a.ts', 'src')).toBe(false)
  })

  it('searches everything when no scope is given', () => {
    expect(matchesScope('docs/a.md', '')).toBe(true)
    expect(matchesScope('docs/a.md', undefined)).toBe(true)
  })
})

describe('relative paths', () => {
  it('handles posix and windows spellings', () => {
    expect(relativeToRoot('/w/proj', '/w/proj/src/a.ts')).toBe('src/a.ts')
    expect(relativeToRoot('/w/proj/', '/w/proj/src/a.ts')).toBe('src/a.ts')
    expect(relativeToRoot('/w/proj', '/w/other/a.ts')).toBeNull()
    expect(relativeToRoot('C:\\proj', 'C:\\proj\\src\\a.ts')).toBe('src/a.ts')
    expect(relativeToRoot('C:\\Proj', 'c:\\proj\\SRC\\a.ts')).toBe('SRC/a.ts')
  })
})

describe('query matching', () => {
  it('treats a literal query as text, not a pattern', () => {
    const re = compileQuery('a.b')

    expect(re.test('a.b')).toBe(true)
    expect(re.test('axb')).toBe(false)
  })

  it('surfaces an invalid regex as a throw the pane can show', () => {
    expect(() => compileQuery('([', { regex: true })).toThrow()
  })

  it('reports 1-based lines, 0-based columns and respects the per-file cap', () => {
    const text = 'alpha\n  TARGET here\nnope\nTARGET again\n'
    const capped = findLineMatches(text, compileQuery('TARGET'), 1)

    expect(capped).toHaveLength(1)
    expect({ col: capped[0].col, line: capped[0].line }).toEqual({ col: 2, line: 2 })
    expect(findLineMatches(text, compileQuery('TARGET'), 5).map(hit => hit.line)).toEqual([2, 4])
  })
})

describe('runFileSearch over a real tree', () => {
  let root = ''

  beforeAll(async () => {
    root = await mkdtemp(path.join(tmpdir(), 'hermes-files-search-'))
    await mkdir(path.join(root, 'src/deep'), { recursive: true })
    await mkdir(path.join(root, 'docs'), { recursive: true })
    await mkdir(path.join(root, 'other'), { recursive: true })
    await writeFile(path.join(root, 'src/deep/app.py'), 'import os\nTARGET_FIELD = 1\n')
    await writeFile(path.join(root, 'src/deep/view.ts'), '// TARGET_FIELD in ts\n')
    await writeFile(path.join(root, 'docs/readme.md'), 'TARGET_FIELD doc\n')
    await writeFile(path.join(root, 'other/notes.py'), 'TARGET_FIELD other\n')
    await writeFile(path.join(root, 'asset.bin'), 'TARGET_FIELD binary-ish\n')
    // Larger than this fixture io's read cap: the search must say the tail was not read.
    await writeFile(path.join(root, 'huge.log'), `TARGET_FIELD\n${'x'.repeat(5000)}\n`)
  })

  afterAll(async () => {
    await rm(root, { force: true, recursive: true })
  })

  it('finds a file name inside a folder the tree never expanded', async () => {
    const out = await runFileSearch({ mode: 'name', query: 'app.py', root }, fsIo)

    expect(out.hits.map(hit => hit.rel)).toEqual(['src/deep/app.py'])
  })

  it('limits a name search to the requested folder', async () => {
    const out = await runFileSearch({ mode: 'name', query: '.py', root, scope: 'src' }, fsIo)

    expect(out.hits.map(hit => hit.rel)).toEqual(['src/deep/app.py'])
  })

  it('returns path, line, column and snippet for a content match', async () => {
    const out = await runFileSearch({ mode: 'content', query: 'TARGET_FIELD', root }, fsIo)
    const hit = out.hits.find(entry => entry.rel === 'src/deep/app.py')

    expect(hit).toBeTruthy()
    expect({ col: hit?.col, line: hit?.line }).toEqual({ col: 0, line: 2 })
    expect(hit?.text).toContain('TARGET_FIELD')
  })

  it('applies a glob scope to a content search', async () => {
    const out = await runFileSearch({ mode: 'content', query: 'TARGET_FIELD', root, scope: 'src/**/*.py' }, fsIo)

    expect(out.hits.map(hit => hit.rel)).toEqual(['src/deep/app.py'])
  })

  it('skips binary extensions instead of reporting a clean no-match', async () => {
    const out = await runFileSearch({ mode: 'content', query: 'TARGET_FIELD', root }, fsIo)

    expect(out.hits.some(hit => hit.rel === 'asset.bin')).toBe(false)
    expect(out.skipped).toBeGreaterThan(0)
  })

  it('marks the outcome incomplete when a read was cut off', async () => {
    const out = await runFileSearch({ mode: 'content', query: 'TARGET_FIELD', root }, fsIo)

    expect(out.hits.some(hit => hit.rel === 'huge.log')).toBe(true)
    expect(out.truncated).toBe(true)
  })

  it('cuts the result list at maxMatches and says so', async () => {
    const out = await runFileSearch({ limits: { maxMatches: 1 }, mode: 'content', query: 'TARGET_FIELD', root }, fsIo)

    expect(out.hits).toHaveLength(1)
    expect(out.truncated).toBe(true)
  })

  it('stops the walk at maxDirs and says so', async () => {
    const out = await runFileSearch({ limits: { maxDirs: 1 }, mode: 'name', query: '.', root }, fsIo)

    expect(out.truncated).toBe(true)
    expect(out.dirsScanned).toBeLessThanOrEqual(2)
  })

  it('returns nothing for an aborted signal', async () => {
    const controller = new AbortController()

    controller.abort()

    const out = await runFileSearch({ mode: 'content', query: 'TARGET_FIELD', root }, fsIo, controller.signal)

    expect(out.hits).toHaveLength(0)
  })

  it('does not walk for an empty query or an empty root', async () => {
    expect((await runFileSearch({ mode: 'name', query: '   ', root }, fsIo)).dirsScanned).toBe(0)
    expect((await runFileSearch({ mode: 'content', query: 'x', root: '' }, fsIo)).dirsScanned).toBe(0)
  })

  it('groups hits per file in line order', async () => {
    const out = await runFileSearch({ mode: 'content', query: 'TARGET_FIELD', root }, fsIo)
    const groups = groupHitsByFile(out.hits)

    expect(new Set(groups.map(group => group.path)).size).toBe(groups.length)

    for (const group of groups) {
      expect(group.hits.map(hit => hit.line ?? 0)).toEqual(group.hits.map(hit => hit.line ?? 0).sort((a, b) => a - b))
    }
  })
})
