import fs from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'

import { Minimatch } from 'minimatch'

import type { FileSearchRequest } from '../../shared/src/file-search'

// Mirrors src/lib/excluded-paths.ts: the eye toggle may reveal project ignores,
// but never dependency/VCS/cache trees stripped by the native file browser.
const ALWAYS_HIDDEN = [
  '.git',
  '.hg',
  '.svn',
  'node_modules',
  'bower_components',
  '.venv',
  'venv',
  'env',
  '__pycache__',
  '.mypy_cache',
  '.pytest_cache',
  '.ruff_cache',
  '.tox',
  '.gradle',
  '.idea',
  'dist',
  'build',
  'out',
  'target',
  'vendor',
  'Pods',
  '.next',
  '.nuxt',
  '.svelte-kit',
  '.output',
  '.turbo',
  '.parcel-cache',
  '.cache',
  '.terraform',
  '.expo',
  '.angular',
  'coverage',
  '.DS_Store',
  'Thumbs.db'
]

const REVEALABLE = new Set(['env', 'out', 'vendor', 'coverage'])
const SECRET_DIRECTORIES = new Set(['.ssh', '.aws', '.gnupg', 'mcp-tokens', 'pairing'])

// Includes web_routers/files.py + home_data_layout.SECRET_CACHE_STORES.
const SECRET_NAMES = new Set([
  '.env',
  '.envrc',
  'auth.json',
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
  'photon-sidecar.json'
])

export interface SearchScope {
  root: string
  globs: string[]
  accepts: (relative: string) => boolean
  blocked: boolean
}

function isWithin(root: string, target: string): boolean {
  const relative = path.relative(root, target)

  return relative === '' || (!path.isAbsolute(relative) && relative !== '..' && !relative.startsWith(`..${path.sep}`))
}

function escapeGlob(literal: string): string {
  return literal.replace(/[\\*?[\]{}!]/g, '\\$&')
}

function isSecret(part: string): boolean {
  const lower = part.toLowerCase()

  return SECRET_DIRECTORIES.has(lower) || SECRET_NAMES.has(lower) || lower.startsWith('.env.')
}

/** Negative-only pruning preserves .gitignore precedence (positive rg globs override it). */
async function scopePruningGlobs(root: string, staticPrefix: string[]): Promise<string[]> {
  let parent = root
  let relative = ''
  const globs: string[] = []
  let argvSize = 0

  for (const segment of staticPrefix) {
    const directory = await fs.opendir(parent, { bufferSize: 32 })

    for await (const entry of directory) {
      const sibling = entry.name

      if (sibling === segment) {
        continue
      }

      const glob = `!/${relative}${escapeGlob(sibling)}`
      argvSize += glob.length + 4

      // Windows has a small command-line ceiling; post-filtering remains exact.
      if (argvSize > 12000) {
        return []
      }

      globs.push(glob)
    }

    parent = path.join(parent, segment)
    relative += `${escapeGlob(segment)}/`
  }

  return globs
}

async function rejectSymlinkScope(root: string, relative: string): Promise<void> {
  let current = root

  for (const segment of relative.split('/').filter(Boolean)) {
    current = path.join(current, segment)

    if ((await fs.lstat(current)).isSymbolicLink()) {
      throw new Error('Scope must not traverse a symbolic link or junction')
    }
  }

  if (!isWithin(root, await fs.realpath(current))) {
    throw new Error('Scope resolves outside the search root')
  }
}

async function canonicalConfig(home: string): Promise<string> {
  const canonicalHome = await fs.realpath(home).catch((error: NodeJS.ErrnoException) => {
    if (error.code === 'ENOENT') {return path.resolve(home)}
    throw error
  })

  const config = path.join(canonicalHome, 'config.yaml')

  return fs.realpath(config).catch((error: NodeJS.ErrnoException) => {
    if (error.code === 'ENOENT') {return config}
    throw error
  })
}

async function privateConfigs(privateHomes?: string[]): Promise<string[]> {
  const homes = [
    ...new Set([
      ...(privateHomes ?? []),
      process.env.HERMES_HOME || path.join(os.homedir(), '.hermes'),
      path.join(os.homedir(), '.hermes')
    ])
  ]

  const configs: string[] = []

  for (const home of homes) {
    configs.push(await canonicalConfig(home))

    const profiles = await fs.readdir(path.join(home, 'profiles'), { withFileTypes: true }).catch((error: NodeJS.ErrnoException) => {
      if (error.code === 'ENOENT') {return []}
      throw error
    })

    for (const profile of profiles) {
      if (profile.isDirectory() || profile.isSymbolicLink()) {configs.push(await canonicalConfig(path.join(home, 'profiles', profile.name)))}
    }
  }

  return configs
}

async function resolveScopeTarget(root: string, rawScope: string) {
  const portable = (process.platform === 'win32' ? rawScope.replace(/\\/g, '/') : rawScope).replace(/^(?:\.\/)+/, '')

  if (
    portable.split('/').includes('..') ||
    portable.startsWith('!') ||
    (/^[a-z]:/i.test(portable) && !path.win32.isAbsolute(rawScope))
  ) {
    throw new Error('Scope must stay within the search root')
  }

  const absolute = path.isAbsolute(rawScope) || path.win32.isAbsolute(rawScope)

  if (absolute && !path.isAbsolute(rawScope)) {
    throw new Error('Scope must use native paths on this host')
  }

  const target = absolute ? rawScope : path.resolve(root, portable || '.')

  if (!isWithin(root, target)) {
    throw new Error('Scope is outside the search root')
  }

  // Existing files (including [brackets] or braces) take precedence over glob syntax.
  const existing = await fs.lstat(target).catch((error: NodeJS.ErrnoException) => {
    if (error.code === 'ENOENT') {
      return undefined
    }

    throw error
  })

  const glob = !existing && /[*?[\]{}]/.test(portable)

  if (glob && absolute) {
    throw new Error('Glob scopes must be relative to the search root')
  }

  const relative = glob ? portable : path.relative(root, target).split(path.sep).join('/')
  const staticParts = relative.split('/').filter(Boolean)
  const firstGlob = staticParts.findIndex(part => /[*?[\]{}]/.test(part))
  const prefix = glob ? staticParts.slice(0, firstGlob) : staticParts
  await rejectSymlinkScope(root, prefix.join('/'))
  const directory = !glob && (await fs.stat(target)).isDirectory()

  return { prefix, relative, glob, directory }
}

export async function resolveSearchScope(request: FileSearchRequest, privateHomes?: string[]): Promise<SearchScope> {
  if (!path.isAbsolute(request.root) || /^\\\\[?.]\\/.test(request.root)) {
    throw new Error('Search root must be an absolute native directory path (not a device path)')
  }

  const root = await fs.realpath(request.root)

  if (!(await fs.stat(root)).isDirectory()) {
    throw new Error('Search root must be a directory')
  }

  const configs = await privateConfigs(privateHomes)

  const hidden = ALWAYS_HIDDEN.filter(name => !request.showIgnored || !REVEALABLE.has(name))
  const excluded = new Set(hidden.map(name => name.toLowerCase()))
  const globs = [...hidden, ...SECRET_DIRECTORIES, ...SECRET_NAMES, '.env.*'].flatMap(name => ['--iglob', `!${name}`])

  const privateRelative = configs
    .filter(config => isWithin(root, config))
    .map(config => path.relative(root, config).split(path.sep).join('/'))

  for (const config of privateRelative) {globs.push('--iglob', `!/${escapeGlob(config)}`)}
  const { prefix, relative, glob, directory } = await resolveScopeTarget(root, request.scope || '')
  globs.push(...(await scopePruningGlobs(root, prefix)).flatMap(pattern => ['--glob', pattern]))

  // Only bounded, root-relative globs reach the host matcher. Name-query regexes
  // run in rg, never in this main process.
  const matcher = glob ? new Minimatch(relative.includes('/') ? relative : `**/${relative}`, { dot: true, noext: true, nonegate: true, nocomment: true, braceExpandMax: 128 }) : null

  const matches = (rel: string) =>
    !relative ||
    (glob
      ? matcher!.match(rel)
      : rel === relative || (directory && rel.startsWith(`${relative}/`)))

  return {
    root,
    globs,
    blocked: [...root.split(path.sep), ...request.root.split(/[/\\]/)].some(isSecret),
    accepts: rel =>
      !privateRelative.includes(rel) &&
      !rel.split('/').some(part => isSecret(part) || excluded.has(part.toLowerCase())) &&
      matches(rel)
  }
}
