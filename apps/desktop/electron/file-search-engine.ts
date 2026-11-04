import { spawn } from 'node:child_process'
import type { ChildProcess } from 'node:child_process'
import path from 'node:path'
import { performance } from 'node:perf_hooks'

import type { FileSearchFrame, FileSearchMatch, FileSearchRequest } from '../../shared/src/file-search'
import { validateFileSearchRequest } from '../../shared/src/file-search'

import { resolveSearchScope } from './file-search-engine-paths'
import type { SearchScope } from './file-search-engine-paths'
import { contentHit, relativeRgPath, RgRecordReader } from './file-search-engine-records'

export interface NativeFileSearchOptions {
  /** Resolved by the native host; this module never discovers or installs rg. */
  rgPath: string
  privateHomes?: string[]
  signal?: AbortSignal
  onFrame: (frame: FileSearchFrame) => void
}

interface ProcessState {
  child: ChildProcess
  code?: number | null
  error?: Error
  closed: boolean
}

function queryArgs(request: FileSearchRequest): string[] {
  return [
    request.caseSensitive ? '--case-sensitive' : '--ignore-case',
    ...(request.regex ? [] : ['--fixed-strings']),
    '-e',
    request.query
  ]
}

async function prepareScope(
  request: FileSearchRequest,
  signal: AbortSignal | undefined,
  started: number,
  privateHomes?: string[]
): Promise<SearchScope | undefined> {
  let timer: ReturnType<typeof setTimeout>
  let abort: () => void

  const cancelled = new Promise<undefined>(resolve => {
    abort = () => resolve(undefined)
    signal?.addEventListener('abort', abort, { once: true })

    if (signal?.aborted) {
      abort()
    }

    timer = setTimeout(abort, Math.max(0, request.timeoutMs! - (performance.now() - started)))
  })

  try {
    return await Promise.race([resolveSearchScope(request, privateHomes), cancelled])
  } finally {
    clearTimeout(timer!)
    signal?.removeEventListener('abort', abort!)
  }
}

/** Native-only primitive: no gateway, path bridge, IPC or executable acquisition.
 * Resolves only after all rg children close. Abort emits no further frames.
 * A done frame with truncated=true is deliberate early termination or partial I/O;
 * an error frame is a failed engine/validation invocation, possibly after batches.
 */
export async function searchNativeFiles(input: FileSearchRequest, options: NativeFileSearchOptions): Promise<void> {
  const started = performance.now()

  if (options.signal?.aborted) {
    return
  }

  let request: FileSearchRequest
  let scope: SearchScope | undefined

  try {
    request = validateFileSearchRequest(input)

    if (!path.isAbsolute(options.rgPath) || options.rgPath.includes('\0')) {
      throw new Error('rgPath must be an absolute injected native executable path')
    }

    scope = await prepareScope(request, options.signal, started, options.privateHomes)
  } catch (error) {
    if (!options.signal?.aborted) {
      options.onFrame({ type: 'error', code: 'invalid-request', message: (error as Error).message })
    }

    return
  }

  if (options.signal?.aborted) {
    return
  }

  if (!scope || scope.blocked) {
    options.onFrame({
      type: 'done',
      truncated: !scope,
      ...(!scope ? { reason: 'timeout' } : {}),
      warnings: [],
      elapsedMs: performance.now() - started
    })

    return
  }

  await runSearch(request, scope, options, started)
}

function runSearch(
  request: FileSearchRequest,
  scope: SearchScope,
  options: NativeFileSearchOptions,
  started: number
): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    const processes: ProcessState[] = []
    let pending: FileSearchMatch[] = []
    let total = 0
    let filesSearched: number | undefined
    let reason: string | undefined
    let failure: { code: string; message: string } | undefined
    let callbackError: unknown
    let callbackFailed = false
    let stderr = ''
    let killTimer: ReturnType<typeof setTimeout> | undefined
    let finished = false

    const stop = (why: string) => {
      if (!reason) {
        reason = why
      }

      for (const { child, closed } of processes) {
        if (!closed) {
          child.stdout?.resume()
          child.stdin?.destroy()
          child.kill('SIGTERM')
        }
      }

      if (!killTimer) {
        killTimer = setTimeout(() => {
          for (const { child, closed } of processes) {
            if (!closed) {
              child.kill('SIGKILL')
            }
          }
        }, 250)
      }
    }

    const emit = (frame: FileSearchFrame) => {
      if (options.signal?.aborted || callbackFailed) {
        return
      }

      try {
        options.onFrame(frame)
      } catch (error) {
        callbackError = error
        callbackFailed = true
        stop('consumer-error')
      }
    }

    const flush = () => {
      if (pending.length) {
        const hits = pending
        pending = []
        emit({ type: 'batch', hits })
      }
    }

    const fail = (code: string, error: unknown) => {
      failure ||= { code, message: error instanceof Error ? error.message : String(error) }
      stop('engine-error')
    }

    const accept = (hit: FileSearchMatch) => {
      if (reason || !scope.accepts(hit.rel)) {
        return
      }

      pending.push(hit)
      total += 1

      if (pending.length >= 32) {
        flush()
      }

      if (total >= request.maxResults!) {
        stop('max-results')
      }
    }

    const abort = () => {
      pending = []
      stop('aborted')
    }

    const batchTimer = setInterval(flush, 25)
    const timeout = setTimeout(() => stop('timeout'), Math.max(0, request.timeoutMs! - (performance.now() - started)))
    options.signal?.addEventListener('abort', abort, { once: true })

    const finish = () => {
      if (finished || processes.some(state => !state.closed)) {
        return
      }

      finished = true
      clearInterval(batchTimer)
      clearTimeout(timeout)
      clearTimeout(killTimer)
      options.signal?.removeEventListener('abort', abort)

      if (!options.signal?.aborted && !callbackFailed) {
        flush()
        const abnormal = processes.find(state => state.error || (state.code !== 0 && state.code !== 1))

        if (!reason && abnormal) {
          if (total && !abnormal.error && stderr) {
            reason = 'partial-error'
          } else {
            failure = {
              code: 'engine-error',
              message: abnormal.error?.message || stderr.trim() || `rg exited ${abnormal.code}`
            }
          }
        }

        if (failure) {
          emit({ type: 'error', ...failure })
        } else if (!callbackFailed) {
          emit({
            type: 'done',
            truncated: Boolean(reason),
            ...(reason ? { reason } : {}),
            warnings: stderr ? [stderr.trim()] : [],
            elapsedMs: performance.now() - started,
            ...(filesSearched === undefined ? {} : { filesSearched })
          })
        }
      }

      if (callbackFailed) {
        reject(callbackError)
      } else {
        resolve()
      }
    }

    const launch = (args: string[], allowPartialIo = false): ProcessState => {
      const child = spawn(options.rgPath, args, {
        cwd: scope.root,
        shell: false,
        windowsHide: true,
        stdio: ['pipe', 'pipe', 'pipe']
      })

      const state: ProcessState = { child, closed: false }
      processes.push(state)
      child.stderr!.on('data', (chunk: Buffer) => {
        stderr = (stderr + chunk.toString('utf8')).slice(0, 16384)
      })
      child.on('error', error => {
        state.error = error
        fail('engine-error', error)
      })
      child.on('close', code => {
        state.closed = true
        state.code = code

        if (!reason && !allowPartialIo && code !== 0 && code !== 1 && processes.some(other => !other.closed)) {
          failure = { code: 'engine-error', message: state.error?.message || stderr.trim() || `rg exited ${code}` }
          stop('engine-error')
        }

        finish()
      })

      return state
    }

    const read = (state: ProcessState, reader: RgRecordReader) => {
      state.child.stdout!.on('data', (chunk: Buffer) => {
        if (reason) {
          return
        }

        try {
          reader.push(chunk)
        } catch (error) {
          fail('output-limit', error)
        }
      })
      state.child.stdout!.on('end', () => {
        if (reason) {
          return
        }

        try {
          reader.finish()
        } catch (error) {
          fail('invalid-output', error)
        }
      })
    }

    try {
      const common = [
        '--no-config',
        '--hidden',
        '--no-follow',
        '--no-require-git',
        '--color',
        'never',
        '--threads',
        '1',
        ...scope.globs
      ]

      if (request.showIgnored) {
        common.push('--no-ignore')
      }

      if (request.mode === 'content') {
        // Do not use --max-columns: rg JSON would silently omit long-line hits.
        // The byte-record reader instead fails explicitly above its bounded ceiling.
        const engine = launch([...common, '--crlf', '--line-buffered', '--json', ...queryArgs(request), '--', '.'])
        engine.child.stdin!.end()
        read(
          engine,
          new RgRecordReader(10, record => {
            const event = JSON.parse(record.toString('utf8'))

            if (event.type === 'match') {
              accept(contentHit(scope.root, event.data))
            }

            if (event.type === 'summary') {
              filesSearched = event.data.stats?.searches
            }

            return !reason
          })
        )
      } else {
        const filter = launch([
          '--no-config',
          '--hidden',
          '--no-follow',
          '--color',
          'never',
          '--null-data',
          '--line-buffered',
          '--no-filename',
          '--no-line-number',
          '--no-heading',
          ...queryArgs(request),
          '--',
          '-'
        ])

        const walker = launch([...common, '--files', '--null', '--', '.'], true)
        walker.child.stdin!.end()
        filter.child.stdin!.on('error', error => {
          if (!reason && (error as NodeJS.ErrnoException).code !== 'EPIPE') {
            fail('engine-error', error)
          }
        })
        filter.child.stdin!.on('drain', () => {
          if (!reason) {
            walker.child.stdout!.resume()
          }
        })
        read(
          filter,
          new RgRecordReader(0, record => {
            const rel = relativeRgPath(record.toString('utf8'))
            accept({ path: path.join(scope.root, rel), rel })

            return !reason
          })
        )
        read(
          walker,
          new RgRecordReader(0, record => {
            const rel = relativeRgPath(record.toString('utf8'))

            if (scope.accepts(rel)) {
              filesSearched = (filesSearched || 0) + 1

              if (!filter.child.stdin!.write(`${rel}\0`)) {
                walker.child.stdout!.pause()
              }
            }

            return !reason
          })
        )
        walker.child.stdout!.on('end', () => filter.child.stdin!.end())
      }

      if (options.signal?.aborted) {
        abort()
      }
    } catch (error) {
      fail('engine-error', error)
      finish()
    }
  })
}
