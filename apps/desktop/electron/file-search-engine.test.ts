import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterEach, test } from 'vitest'

import type { FileSearchFrame, FileSearchMatch, FileSearchRequest } from '../../shared/src/file-search'

import { contentHit, relativeRgPath, RgRecordReader } from './file-search-engine-records'

const temporaryRoots: string[] = []
const rgName = process.platform === 'win32' ? 'rg.exe' : 'rg'

// Only the test discovers a prerequisite; the engine always receives an explicit executable.
const rgPath =
  process.env.HERMES_TEST_RG ||
  (process.env.PATH || '')
    .split(path.delimiter)
    .map(directory => path.resolve(directory, rgName))
    .find(candidate => fs.existsSync(candidate))!

function fixture(files: Record<string, string>): string {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-native-search-'))
  temporaryRoots.push(root)

  for (const [relative, content] of Object.entries(files)) {
    const target = path.join(root, relative)
    fs.mkdirSync(path.dirname(target), { recursive: true })
    fs.writeFileSync(target, content)
  }

  return root
}

afterEach(() => {
  for (const root of temporaryRoots.splice(0)) {
    fs.rmSync(root, { recursive: true, force: true })
  }
})

async function api() {
  const contract = await import('../../shared/src/file-search').catch(() => null)
  assert.equal(typeof contract?.validateFileSearchRequest, 'function', 'shared request validator must exist')
  const engine = await import('./file-search-engine').catch(() => null)
  assert.equal(typeof engine?.searchNativeFiles, 'function', 'native search entry must exist')
  assert.ok(rgPath, 'real ripgrep is a test prerequisite (HERMES_TEST_RG can supply its absolute path)')

  return { ...contract!, ...engine! }
}

async function collect(request: FileSearchRequest): Promise<FileSearchFrame[]> {
  const { searchNativeFiles } = await api()
  const frames: FileSearchFrame[] = []
  await searchNativeFiles(request, { rgPath, onFrame: frame => frames.push(frame) })

  return frames
}

function hits(frames: FileSearchFrame[]): FileSearchMatch[] {
  return frames.flatMap(frame => (frame.type === 'batch' ? frame.hits : []))
}

function done(frames: FileSearchFrame[]) {
  const terminal = frames.at(-1)
  assert.equal(terminal?.type, 'done', JSON.stringify(frames))

  return terminal as Extract<FileSearchFrame, { type: 'done' }>
}

test('shared validation preserves literal whitespace and fills bounded defaults', async () => {
  const { validateFileSearchRequest } = await api()
  const root = fixture({})
  const request = validateFileSearchRequest({ root, query: '  literal  ', mode: 'content' })
  assert.equal(request.query, '  literal  ')
  assert.equal(request.root, root)
  assert.equal(request.maxResults, 1000)
  assert.equal(request.timeoutMs, 15000)
})

test('real rg maps UTF-8 submatches to UTF-16 columns and CRLF-free snippets', async () => {
  const root = fixture({ '子目录/含空格.ts': 'first\r\n😀中 needle café needle\r\n' })
  const frames = await collect({ root, query: 'needle', mode: 'content', caseSensitive: true })
  assert.deepEqual(hits(frames), [
    {
      path: path.join(root, '子目录/含空格.ts'),
      rel: '子目录/含空格.ts',
      line: 2,
      col: 4,
      text: '😀中 needle café needle',
      ranges: [
        { from: 4, to: 10 },
        { from: 16, to: 22 }
      ]
    }
  ])
  assert.equal(done(frames).truncated, false)
  assert.deepEqual(done(frames).warnings, [])
})

test('shared validator rejects malformed, unbounded and multiline content requests', async () => {
  const { validateFileSearchRequest, FileSearchValidationError } = await api()
  const valid = { root: fixture({}), query: 'x', mode: 'content' }

  for (const patch of [
    { root: '' },
    { root: ' ' },
    { root: 'x\0y' },
    { root: 'a'.repeat(4097) },
    { query: '' },
    { query: 'x\0y' },
    { query: 'a'.repeat(4097) },
    { query: 'a\nb' },
    { query: 'a\rb' },
    { scope: 'x\0y' },
    { scope: 'a'.repeat(4097) },
    { mode: 'other' },
    { regex: 'true' },
    { caseSensitive: 1 },
    { showIgnored: null },
    { maxResults: 0 },
    { maxResults: 2001 },
    { maxResults: 1.5 },
    { maxResults: '10' },
    { timeoutMs: 49 },
    { timeoutMs: 30001 },
    { timeoutMs: NaN }
  ]) {
    assert.throws(() => validateFileSearchRequest({ ...valid, ...patch }), FileSearchValidationError)
  }

  for (const input of [null, [], 'request']) {
    assert.throws(() => validateFileSearchRequest(input), FileSearchValidationError)
  }

  assert.equal(
    validateFileSearchRequest({ ...valid, query: 'x\ny', mode: 'name', maxResults: 2000, timeoutMs: 30000 }).query,
    'x\ny'
  )
})

test('name mode searches relative paths literally or with rg regex and case controls', async () => {
  const root = fixture({ 'src/Hello[1].ts': 'no content match', 'hello.txt': '', '.hidden/HELLO.md': '' })
  const literal = await collect({ root, query: 'Hello[1]', mode: 'name' })
  assert.deepEqual(hits(literal), [{ path: path.join(root, 'src/Hello[1].ts'), rel: 'src/Hello[1].ts' }])
  const folded = await collect({ root, query: 'hello', mode: 'name' })
  assert.deepEqual(
    hits(folded)
      .map(hit => hit.rel)
      .sort(),
    ['.hidden/HELLO.md', 'hello.txt', 'src/Hello[1].ts']
  )

  const regex = await collect({
    root,
    query: '^src/Hello\\[1\\]\\.ts$',
    mode: 'name',
    regex: true,
    caseSensitive: true
  })

  assert.deepEqual(
    hits(regex).map(hit => hit.rel),
    ['src/Hello[1].ts']
  )
  assert.equal(done(regex).truncated, false)
})

test('project configs stay searchable while the actual Hermes home config stays private', async () => {
  const root = fixture({
    'repo/config.yaml': 'needle public',
    'home/config.yaml': 'needle private',
    'home/profiles/work/config.yaml': 'needle private'
  })

  const { searchNativeFiles } = await api()
  const frames: FileSearchFrame[] = []
  await searchNativeFiles(
    { root, query: 'needle', mode: 'content' },
    {
      rgPath,
      privateHomes: [path.join(root, 'home')],
      onFrame: frame => frames.push(frame)
    }
  )
  assert.deepEqual(
    hits(frames).map(hit => hit.rel),
    ['repo/config.yaml']
  )
})

test('private configuration exclusions use canonical home and profile identities', async () => {
  const root = fixture({ 'real-home/config.yaml': 'needle private', 'real-home/profiles/work/config.yaml': 'needle profile', 'public/config.yaml': 'needle public' })
  const linked = path.join(root, 'linked-home')
  fs.symlinkSync(path.join(root, 'real-home'), linked, process.platform === 'win32' ? 'junction' : 'dir')
  const { searchNativeFiles } = await api()

  for (const home of [linked, path.join(linked, 'profiles/work')]) {
    const frames: FileSearchFrame[] = []
    await searchNativeFiles({ root: home, query: 'needle', mode: 'content', showIgnored: true }, {
      rgPath, privateHomes: [home], onFrame: frame => frames.push(frame)
    })
    assert.deepEqual(hits(frames), [])
  }

  const control = await collect({ root: path.join(root, 'public'), query: 'needle', mode: 'content' })
  assert.deepEqual(hits(control).map(hit => hit.rel), ['config.yaml'])
})

test('private profile directory links do not expose their real config paths', async () => {
  const root = fixture({ 'home/config.yaml': 'needle private', 'actual-profile/config.yaml': 'needle private', 'actual-profile/public.py': 'needle public' })
  fs.mkdirSync(path.join(root, 'home/profiles'), { recursive: true })
  fs.symlinkSync(path.join(root, 'actual-profile'), path.join(root, 'home/profiles/work'), process.platform === 'win32' ? 'junction' : 'dir')
  const { searchNativeFiles } = await api()
  const frames: FileSearchFrame[] = []
  await searchNativeFiles({ root: path.join(root, 'actual-profile'), query: 'needle', mode: 'content' }, {
    rgPath, privateHomes: [path.join(root, 'home')], onFrame: frame => frames.push(frame)
  })
  assert.deepEqual(hits(frames).map(hit => hit.rel), ['public.py'])
})

test('glob scopes include allowed dotfiles and dotdirectories', async () => {
  const root = fixture({ 'visible.py': 'needle', '.hidden.py': 'needle', '.config/app.py': 'needle', 'src/.hidden.py': 'needle', 'src/.config/app.py': 'needle' })

  for (const showIgnored of [false, true]) {
    const all = hits(await collect({ root, query: 'needle', mode: 'content', showIgnored })).map(hit => hit.rel).sort()
    const scoped = hits(await collect({ root, query: 'needle', mode: 'content', scope: '*.py', showIgnored })).map(hit => hit.rel).sort()
    assert.deepEqual(scoped, all)
    const deep = hits(await collect({ root, query: 'needle', mode: 'content', scope: 'src/**/*.py', showIgnored })).map(hit => hit.rel).sort()
    assert.deepEqual(deep, all.filter(rel => rel.startsWith('src/')))
  }
})

test('basename globs include matching nested files', async () => {
  const root = fixture({ 'src/nested/a.py': 'needle', 'src/b.ts': 'needle' })
  assert.deepEqual(
    hits(await collect({ root, query: 'needle', mode: 'content', scope: '*.py' })).map(hit => hit.rel),
    ['src/nested/a.py']
  )
})

test('scope selects only its directory, exact file or root-relative glob', async () => {
  const root = fixture({ 'src/a.ts': 'needle', 'src/deep/b.ts': 'needle', 'src2/a.ts': 'needle', 'notes.md': 'needle' })

  for (const mode of ['name', 'content'] as const) {
    const query = mode === 'name' ? '.ts' : 'needle'
    const directory = await collect({ root, query, mode, scope: 'src' })
    assert.deepEqual(
      hits(directory)
        .map(hit => hit.rel)
        .sort(),
      ['src/a.ts', 'src/deep/b.ts']
    )
    const exact = await collect({ root, query, mode, scope: path.join(root, 'src/a.ts') })
    assert.deepEqual(
      hits(exact).map(hit => hit.rel),
      ['src/a.ts']
    )
    const glob = await collect({ root, query, mode, scope: 'src/**/*.ts' })
    assert.deepEqual(
      hits(glob)
        .map(hit => hit.rel)
        .sort(),
      ['src/a.ts', 'src/deep/b.ts']
    )
    assert.equal(done(glob).truncated, false)
  }
})

test('scope cannot traverse outside root or address an absolute or negated glob', async () => {
  const root = fixture({ 'visible.txt': 'needle' })

  for (const scope of ['../outside', 'sub/../../outside', '/outside/*.txt', 'C:\\outside\\*.txt', '!*.txt']) {
    const frames = await collect({ root, query: 'needle', mode: 'content', scope })
    assert.equal(frames.at(-1)?.type, 'error', scope)
    assert.equal(hits(frames).length, 0)
  }
})

test('scope treats existing glob-like filenames literally and rejects drive-relative or dot-negated escapes', async () => {
  const root = fixture({ 'src/a[1].ts': 'needle', 'src/a1.ts': 'needle', 'src/other.ts': 'needle' })

  for (const mode of ['name', 'content'] as const) {
    for (const scope of ['src/a[1].ts', path.join(root, 'src/a[1].ts')]) {
      const frames = await collect({ root, query: mode === 'name' ? '.ts' : 'needle', mode, scope })
      assert.deepEqual(
        hits(frames).map(hit => hit.rel),
        ['src/a[1].ts']
      )
    }
  }

  for (const scope of ['C:outside', './!*.ts', './C:outside']) {
    assert.equal((await collect({ root, query: 'needle', mode: 'content', scope })).at(-1)?.type, 'error')
  }
})

test('showIgnored reveals project ignores but preserves tree noise and sensitive exclusions', async () => {
  const secretNames = [
    '.env',
    '.env.local',
    '.ENV.PRODUCTION',
    '.envrc',
    'Auth.JSON',
    'auth.lock',
    'credentials',
    '.anthropic_oauth.json',
    'google_token.json',
    'google_oauth_pending.json',
    'google_oauth.json',
    'webhook_subscriptions.json',
    '.git-credentials',
    'bws_cache.json',
    'bws_cache.enc.json',
    'op_cache.json',
    'photon-sidecar.json',
    '.ssh/key',
    '.aws/token',
    '.gnupg/key',
    'mcp-tokens/token.json',
    'pairing/token.json'
  ]

  const noise = ['.git/private', 'node_modules/dep.txt', '.venv/lib.py', '.cache/cache.txt', 'dist/output.txt']

  const root = fixture(
    Object.fromEntries([
      ['.gitignore', 'ignored.txt\nvendor/\n'],
      ['visible.txt', 'needle'],
      ['ignored.txt', 'needle'],
      ['vendor/source.txt', 'needle'],
      ...secretNames.map(name => [`nested/${name}`, 'needle']),
      ...noise.map(name => [name, 'needle'])
    ])
  )

  for (const mode of ['name', 'content'] as const) {
    const query = mode === 'name' ? '.txt' : 'needle'
    const ordinary = await collect({ root, query, mode })
    assert.deepEqual(
      hits(ordinary).map(hit => hit.rel),
      ['visible.txt']
    )
    const revealed = await collect({ root, query, mode, showIgnored: true })
    assert.deepEqual(
      hits(revealed)
        .map(hit => hit.rel)
        .sort(),
      ['ignored.txt', 'vendor/source.txt', 'visible.txt']
    )
    assert.deepEqual(done(revealed).warnings, [])
    const scopedIgnored = await collect({ root, query, mode, scope: 'ignored.txt' })
    assert.deepEqual(hits(scopedIgnored), [], 'scope must not implicitly reveal gitignored files')
  }

  const allNames = await collect({ root, query: '.', mode: 'name', showIgnored: true })
  assert.ok(
    hits(allNames).every(hit => ['.gitignore', 'visible.txt', 'ignored.txt', 'vendor/source.txt'].includes(hit.rel))
  )

  for (const scope of ['nested/.env', 'nested/.ssh', 'node_modules', 'nested/Auth.JSON']) {
    assert.deepEqual(hits(await collect({ root, query: 'needle', mode: 'content', scope, showIgnored: true })), [])
  }
})

test('sensitive roots and direct credential scopes remain excluded even with showIgnored', async () => {
  const root = fixture({
    '.ssh/key.txt': 'needle',
    '.env/private.txt': 'needle',
    'auth.json': 'needle',
    'visible.txt': 'needle'
  })

  for (const mode of ['name', 'content'] as const) {
    const query = mode === 'name' ? '.txt' : 'needle'

    for (const sensitiveRoot of ['.ssh', '.env']) {
      const frames = await collect({ root: path.join(root, sensitiveRoot), query, mode, showIgnored: true })
      assert.deepEqual(hits(frames), [])
      assert.equal(done(frames).truncated, false)
    }

    assert.deepEqual(
      hits(
        await collect({ root, query: mode === 'name' ? 'auth' : 'needle', mode, scope: 'auth.json', showIgnored: true })
      ),
      []
    )
  }
})

test('symlinks are never followed, including an explicit file or directory scope', async t => {
  const root = fixture({ 'visible.txt': 'needle' })
  const outside = fixture({ 'outside.txt': 'needle' })

  try {
    fs.symlinkSync(outside, path.join(root, 'outside'), process.platform === 'win32' ? 'junction' : 'dir')
    fs.symlinkSync(path.join(outside, 'outside.txt'), path.join(root, 'outside.txt'), 'file')
  } catch (error) {
    if (['EPERM', 'EACCES'].includes((error as NodeJS.ErrnoException).code || '')) {
      t.skip('native symlink privileges unavailable')

      return
    }

    throw error
  }

  for (const mode of ['name', 'content'] as const) {
    const request = { root, query: mode === 'name' ? '.txt' : 'needle', mode, showIgnored: true }
    assert.deepEqual(
      hits(await collect(request)).map(hit => hit.rel),
      ['visible.txt']
    )

    for (const scope of ['outside', 'outside.txt', 'outside/outside.txt']) {
      const frames = await collect({ ...request, scope })
      assert.deepEqual(hits(frames), [])
      assert.equal(frames.at(-1)?.type, 'error')
    }
  }
})

test('no match is success; bad regex and missing executable are errors; dash-leading queries remain operands', async () => {
  const root = fixture({ '-flag.txt': 'prefix --help suffix', 'regex.txt': 'literal [ needle' })

  for (const mode of ['name', 'content'] as const) {
    const empty = await collect({ root, query: 'no-such-match', mode })
    assert.deepEqual(hits(empty), [])
    assert.equal(done(empty).truncated, false)
    const bad = await collect({ root, query: '[', mode, regex: true })
    assert.equal(bad.at(-1)?.type, 'error')
    const dash = await collect({ root, query: mode === 'name' ? '-flag' : '--help', mode })
    assert.equal(hits(dash).length, 1)
    assert.equal(done(dash).truncated, false)
  }

  const { searchNativeFiles } = await api()
  const frames: FileSearchFrame[] = []
  await searchNativeFiles(
    { root, query: 'x', mode: 'content' },
    { rgPath: path.join(root, 'missing-rg'), onFrame: frame => frames.push(frame) }
  )
  assert.equal(frames.at(-1)?.type, 'error')
})

test('CRLF end anchors and zero-width Unicode matches retain valid snippet ranges', async () => {
  const root = fixture({ 'anchors.txt': '😀 café\r\n' })
  const frames = await collect({ root, query: '$', mode: 'content', regex: true })
  const [match] = hits(frames)
  assert.equal(match.col, '😀 café'.length)
  assert.equal(match.text, '😀 café')
  assert.deepEqual(match.ranges, [{ from: '😀 café'.length, to: '😀 café'.length }])
  assert.equal(done(frames).truncated, false)
})

test('record framing preserves UTF-8 split at every byte and rejects incomplete records', () => {
  const source = '😀中 café needle\r\n'

  const record = Buffer.from(
    JSON.stringify({
      type: 'match',
      data: {
        path: { text: './子目录/😀.txt' },
        lines: { text: source },
        line_number: 2,
        submatches: [{ start: Buffer.byteLength('😀中 café '), end: Buffer.byteLength('😀中 café needle') }]
      }
    }) + '\n'
  )

  let matched = 0

  for (let split = 1; split < record.length; split += 1) {
    const reader = new RgRecordReader(10, value => {
      const event = JSON.parse(value.toString('utf8'))
      const hit = contentHit('/fixture', event.data)
      assert.equal(hit.rel, '子目录/😀.txt')
      assert.equal(hit.col, '😀中 café '.length)
      assert.equal(hit.text!.slice(hit.ranges![0].from, hit.ranges![0].to), 'needle')
      matched += 1

      return true
    })

    reader.push(record.subarray(0, split))
    reader.push(record.subarray(split))
    reader.finish()
  }

  assert.equal(matched, record.length - 1)
  const incomplete = new RgRecordReader(10, () => true)
  incomplete.push(Buffer.from('unfinished'))
  assert.throws(() => incomplete.finish(), /incomplete/)

  for (const invalid of ['../outside', '/absolute', 'C:\\outside', './dir/../../outside']) {
    assert.throws(() => relativeRgPath(invalid))
  }
})

test('literal whitespace, literal metacharacters and name newlines survive the native argv boundary', async () => {
  const literalName = process.platform === 'win32' ? 'literal.txt' : 'a\nb.txt'
  const root = fixture({ [literalName]: '  needle  \n.* [ literal\n', 'a.txt': 'needle\n', 'b.txt': '' })
  const spaced = await collect({ root, query: '  needle  ', mode: 'content' })
  assert.deepEqual(
    hits(spaced).map(hit => hit.rel),
    [literalName]
  )
  const literal = await collect({ root, query: '.* [', mode: 'content' })
  assert.deepEqual(
    hits(literal).map(hit => hit.rel),
    [literalName]
  )

  if (process.platform !== 'win32') {
    const name = await collect({ root, query: 'a\nb', mode: 'name' })
    assert.deepEqual(
      hits(name).map(hit => hit.rel),
      [literalName]
    )
  }
})

test('long lines center snippets on a late Unicode match without losing UTF-16 source columns', async () => {
  const prefix = '😀'.repeat(12000)
  const root = fixture({ 'long.txt': `${prefix}needle${'中'.repeat(12000)}\r\n` })
  const frames = await collect({ root, query: 'needle', mode: 'content' })
  const [match] = hits(frames)
  assert.equal(match.col, prefix.length)
  assert.ok(match.text!.length <= 512, `snippet is bounded: ${match.text!.length}`)
  assert.equal(match.text!.slice(match.ranges![0].from, match.ranges![0].to), 'needle')
  assert.ok(!match.text!.includes('\r'))
  assert.ok(!/^[\uDC00-\uDFFF]|[\uD800-\uDBFF]$/.test(match.text!), 'snippet boundaries preserve surrogate pairs')
  assert.equal(done(frames).truncated, false)
})

test('concurrent native searches keep their roots, queries and result limits isolated', async () => {
  const firstRoot = fixture({ 'a.txt': 'alpha\n'.repeat(40) })
  const secondRoot = fixture({ 'b.txt': 'beta\n'.repeat(40) })

  const [first, second] = await Promise.all([
    collect({ root: firstRoot, query: 'alpha', mode: 'content', maxResults: 3 }),
    collect({ root: secondRoot, query: 'beta', mode: 'content', maxResults: 5 })
  ])

  assert.equal(hits(first).length, 3)
  assert.equal(hits(second).length, 5)
  assert.ok(hits(first).every(hit => hit.path === path.join(firstRoot, 'a.txt') && hit.text === 'alpha'))
  assert.ok(hits(second).every(hit => hit.path === path.join(secondRoot, 'b.txt') && hit.text === 'beta'))
  assert.equal(done(first).reason, 'max-results')
  assert.equal(done(second).reason, 'max-results')
})

test('maxResults bounds emitted hits and reports intentional truncation', async () => {
  const root = fixture({ 'many.txt': 'needle\n'.repeat(3000) })
  const frames = await collect({ root, query: 'needle', mode: 'content', maxResults: 7 })
  assert.equal(hits(frames).length, 7)
  assert.equal(done(frames).truncated, true)
  assert.equal(done(frames).reason, 'max-results')
})
