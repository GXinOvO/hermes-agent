import assert from 'node:assert/strict'
import type * as childProcess from 'node:child_process'
import fs from 'node:fs'
import { createRequire, syncBuiltinESMExports } from 'node:module'
import os from 'node:os'
import path from 'node:path'

import { afterEach, test, vi } from 'vitest'

import type { FileSearchFrame } from '../../shared/src/file-search'

import { searchNativeFiles } from './file-search-engine'

const roots: string[] = []
const rgName = process.platform === 'win32' ? 'rg.exe' : 'rg'

const rgPath =
  process.env.HERMES_TEST_RG ||
  (process.env.PATH || '')
    .split(path.delimiter)
    .map(directory => path.resolve(directory, rgName))
    .find(candidate => fs.existsSync(candidate))!

function fixture(content: string): string {
  assert.ok(rgPath, 'real ripgrep is required')
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-search-process-'))
  roots.push(root)
  fs.writeFileSync(path.join(root, 'data.txt'), content)

  return root
}

function observeChildren(onSpawn?: (child: childProcess.ChildProcess) => void) {
  const states: {
    child: childProcess.ChildProcess
    closed: boolean
    exited: boolean
    args: readonly string[]
    options: childProcess.SpawnOptions
  }[] = []

  const mutable = createRequire(import.meta.url)('node:child_process') as typeof childProcess
  const realSpawn = mutable.spawn

  const observedSpawn = (
    executable: string,
    args: readonly string[] = [],
    options: childProcess.SpawnOptions = {}
  ): childProcess.ChildProcess => {
    const child = realSpawn(executable, args, options)
    const state = { child, closed: false, exited: false, args, options }
    states.push(state)
    child.on('close', () => {
      state.closed = true
    })
    child.on('exit', () => {
      state.exited = true
    })
    onSpawn?.(child)

    return child
  }

  vi.spyOn(mutable, 'spawn').mockImplementation(observedSpawn as typeof childProcess.spawn)
  syncBuiltinESMExports()

  return states
}

afterEach(() => {
  vi.restoreAllMocks()
  syncBuiltinESMExports()

  for (const root of roots.splice(0)) {
    fs.rmSync(root, { recursive: true, force: true })
  }
})

test('real rg batches become visible while the child process is still alive', async () => {
  const root = fixture('😀 needle café\r\n'.repeat(150000))
  const states = observeChildren()
  let liveOnFirstBatch = false
  const frames: FileSearchFrame[] = []
  await searchNativeFiles(
    { root, query: 'needle', mode: 'content', maxResults: 96 },
    {
      rgPath,
      onFrame: frame => {
        frames.push(frame)

        if (frame.type === 'batch' && frames.filter(item => item.type === 'batch').length === 1) {
          liveOnFirstBatch = states.some(state => !state.exited && !state.closed && state.child.pid)

          for (const state of states) {
            if (!state.exited && state.child.pid) {
              process.kill(state.child.pid, 0)
            }
          }
        }
      }
    }
  )
  assert.ok(liveOnFirstBatch, 'a real rg child must still be running at the first delivered batch')
  assert.ok(
    states.every(state => state.closed),
    'promise waits for close, not just kill or exit'
  )
  assert.equal(frames.flatMap(frame => (frame.type === 'batch' ? frame.hits : [])).length, 96)

  for (const { args, options } of states) {
    assert.equal(options.shell, false)
    assert.equal(options.windowsHide, true)
    assert.ok(args.includes('--no-config'))
    assert.ok(args.includes('--hidden'))
    assert.ok(args.includes('--no-follow'))
    assert.ok(!args.includes('--max-columns'))
  }
})

test('abort during a streamed batch kills real rg and emits nothing after abort', async () => {
  const root = fixture('needle\n'.repeat(200000))
  const states = observeChildren()
  const controller = new AbortController()
  const frames: FileSearchFrame[] = []
  let frameCountAtAbort = -1
  await searchNativeFiles(
    { root, query: 'needle', mode: 'content' },
    {
      rgPath,
      signal: controller.signal,
      onFrame: frame => {
        frames.push(frame)

        if (frame.type === 'batch' && !controller.signal.aborted) {
          frameCountAtAbort = frames.length
          controller.abort()
        }
      }
    }
  )
  assert.ok(frameCountAtAbort > 0)
  assert.equal(frames.length, frameCountAtAbort)
  assert.ok(states.length > 0)
  assert.ok(
    states.every(state => state.closed),
    'all children are closed before abort resolves'
  )

  for (const { child } of states) {
    if (child.pid) {
      assert.throws(() => process.kill(child.pid!, 0), { code: 'ESRCH' })
    }
  }
})

test('an already-aborted request neither spawns nor emits', async () => {
  const states = observeChildren()
  const controller = new AbortController()
  controller.abort()
  const frames: FileSearchFrame[] = []
  await searchNativeFiles(
    { root: fixture('needle'), query: 'needle', mode: 'name' },
    {
      rgPath,
      signal: controller.signal,
      onFrame: frame => frames.push(frame)
    }
  )
  assert.equal(states.length, 0)
  assert.deepEqual(frames, [])
})

test('giant rg JSON lines fail explicitly at the record budget and clean up the child', async () => {
  const states = observeChildren()
  const frames: FileSearchFrame[] = []
  await searchNativeFiles(
    { root: fixture(`${'a'.repeat(3 * 1024 * 1024)}needle\n`), query: 'needle', mode: 'content' },
    {
      rgPath,
      onFrame: frame => frames.push(frame)
    }
  )
  assert.deepEqual(
    frames.map(frame => frame.type),
    ['error']
  )
  assert.equal((frames[0] as Extract<FileSearchFrame, { type: 'error' }>).code, 'output-limit')
  assert.ok(states.every(state => state.closed))
})

test('partial permission failure retains readable matches and an explicit incomplete warning', async t => {
  if (process.platform === 'win32' || process.getuid?.() === 0) {
    t.skip('requires native POSIX permissions without root bypass')

    return
  }

  const root = fixture('needle readable\n')
  const denied = path.join(root, 'denied.txt')
  fs.writeFileSync(denied, 'needle private\n')
  fs.chmodSync(denied, 0)

  try {
    const frames: FileSearchFrame[] = []
    await searchNativeFiles(
      { root, query: 'needle', mode: 'content' },
      { rgPath, onFrame: frame => frames.push(frame) }
    )
    assert.deepEqual(
      frames.flatMap(frame => (frame.type === 'batch' ? frame.hits.map(hit => hit.rel) : [])),
      ['data.txt']
    )
    const done = frames.at(-1) as Extract<FileSearchFrame, { type: 'done' }>
    assert.equal(done.type, 'done')
    assert.equal(done.truncated, true)
    assert.equal(done.reason, 'partial-error')
    assert.match(done.warnings.join('\n'), /Permission denied|permission denied/)
    assert.ok(done.warnings.join('\n').includes('denied.txt'))
  } finally {
    fs.chmodSync(denied, 0o600)
  }
})

test('native validation refuses foreign roots and unresolved executable names before spawning', async () => {
  const states = observeChildren()

  for (const [root, executable] of [
    ['relative/root', rgPath],
    ['C:\\foreign-root', rgPath],
    [fixture('needle'), 'rg']
  ] as const) {
    const frames: FileSearchFrame[] = []
    await searchNativeFiles(
      { root, query: 'needle', mode: 'content' },
      { rgPath: executable, onFrame: frame => frames.push(frame) }
    )
    assert.deepEqual(
      frames.map(frame => frame.type),
      ['error']
    )
  }

  assert.equal(states.length, 0)
})

test('an I/O failure without matches is an engine error rather than an empty success', async t => {
  if (process.platform === 'win32' || process.getuid?.() === 0) {
    t.skip('requires native POSIX permissions without root bypass')

    return
  }

  const root = fixture('nothing to match')
  const denied = path.join(root, 'denied')
  fs.mkdirSync(denied)
  fs.writeFileSync(path.join(denied, 'private.txt'), 'needle')
  fs.chmodSync(denied, 0)

  try {
    const frames: FileSearchFrame[] = []
    await searchNativeFiles(
      { root, query: 'needle', mode: 'content' },
      { rgPath, onFrame: frame => frames.push(frame) }
    )
    assert.deepEqual(
      frames.map(frame => frame.type),
      ['error']
    )
    assert.match(
      (frames[0] as Extract<FileSearchFrame, { type: 'error' }>).message,
      /Permission denied|permission denied/
    )
  } finally {
    fs.chmodSync(denied, 0o700)
  }
})

test('a consumer callback exception rejects only after child cleanup', async () => {
  const states = observeChildren()
  const sentinel = new Error('consumer failed')
  await assert.rejects(
    searchNativeFiles(
      { root: fixture('needle\n'.repeat(200000)), query: 'needle', mode: 'content' },
      {
        rgPath,
        onFrame: () => {
          throw sentinel
        }
      }
    ),
    error => error === sentinel
  )
  assert.ok(states.length > 0)
  assert.ok(states.every(state => state.closed))
})

test('falsy callback exceptions are not swallowed', async () => {
  let rejected = false

  try {
    await searchNativeFiles(
      { root: fixture('needle'), query: 'needle', mode: 'content' },
      {
        rgPath,
        onFrame: () => {
          throw null
        }
      }
    )
  } catch (error) {
    assert.equal(error, null)
    rejected = true
  }

  assert.equal(rejected, true)
})

test('a timeout kills a stopped real rg, escalates if needed, and awaits close', async t => {
  if (process.platform === 'win32') {
    t.skip('SIGSTOP is a POSIX-only lifecycle fault')

    return
  }

  const states = observeChildren(child => {
    child.kill('SIGSTOP')
  })

  const frames: FileSearchFrame[] = []
  const started = performance.now()
  await searchNativeFiles(
    { root: fixture('needle'), query: 'needle', mode: 'content', timeoutMs: 500 },
    {
      rgPath,
      onFrame: frame => frames.push(frame)
    }
  )
  const done = frames.at(-1) as Extract<FileSearchFrame, { type: 'done' }>
  assert.equal(done.type, 'done')
  assert.equal(done.truncated, true)
  assert.equal(done.reason, 'timeout')
  assert.ok(states.length > 0, 'the test must timeout a live child, not preflight')
  assert.ok(states.every(state => state.closed))
  assert.ok(performance.now() - started < 2500, 'deadline plus kill escalation remains bounded')

  for (const { child } of states) {
    if (child.pid) {
      assert.throws(() => process.kill(child.pid!, 0), { code: 'ESRCH' })
    }
  }
})

test('name cancellation closes both real enumeration and regex-filter rg children', async t => {
  if (process.platform === 'win32') {
    t.skip('SIGSTOP is a POSIX-only lifecycle fault')

    return
  }

  const controller = new AbortController()
  let spawnCount = 0

  const states = observeChildren(child => {
    child.kill('SIGSTOP')

    if (++spawnCount === 2) {
      queueMicrotask(() => controller.abort())
    }
  })

  const frames: FileSearchFrame[] = []
  await searchNativeFiles(
    { root: fixture('needle'), query: 'data', mode: 'name' },
    {
      rgPath,
      signal: controller.signal,
      onFrame: frame => frames.push(frame)
    }
  )
  assert.equal(states.length, 2)
  assert.deepEqual(frames, [])
  assert.ok(states.every(state => state.closed))

  for (const { child } of states) {
    if (child.pid) {
      assert.throws(() => process.kill(child.pid!, 0), { code: 'ESRCH' })
    }
  }
})

test('name enumeration retains partial permission warnings alongside readable names', async t => {
  if (process.platform === 'win32' || process.getuid?.() === 0) {
    t.skip('requires native POSIX permissions without root bypass')

    return
  }

  const root = fixture('needle')
  const denied = path.join(root, 'denied')
  fs.mkdirSync(denied)
  fs.writeFileSync(path.join(denied, 'private.txt'), 'needle')
  fs.chmodSync(denied, 0)

  try {
    const frames: FileSearchFrame[] = []
    await searchNativeFiles({ root, query: '.txt', mode: 'name' }, { rgPath, onFrame: frame => frames.push(frame) })
    assert.deepEqual(
      frames.flatMap(frame => (frame.type === 'batch' ? frame.hits.map(hit => hit.rel) : [])),
      ['data.txt']
    )
    const done = frames.at(-1) as Extract<FileSearchFrame, { type: 'done' }>
    assert.equal(done.type, 'done')
    assert.equal(done.truncated, true)
    assert.equal(done.reason, 'partial-error')
    assert.match(done.warnings.join('\n'), /Permission denied|permission denied/)
  } finally {
    fs.chmodSync(denied, 0o700)
  }
})
