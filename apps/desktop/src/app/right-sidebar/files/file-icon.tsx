import { Codicon } from '@/components/ui/codicon'
import {
  BrandCpp,
  BrandDocker,
  BrandJavascript,
  BrandNpm,
  BrandPython,
  BrandReact,
  BrandTypescript,
  type IconComponent,
  LetterC
} from '@/lib/icons'
import { cn } from '@/lib/utils'

interface FileGlyph {
  className: string
  codicon?: string
  icon?: IconComponent
}

const GLYPHS = {
  c: { className: 'text-(--file-icon-blue)', icon: LetterC },
  config: { className: 'text-(--file-icon-purple)', codicon: 'settings-gear' },
  cpp: { className: 'text-(--file-icon-blue)', icon: BrandCpp },
  docker: { className: 'text-(--ui-cyan)', icon: BrandDocker },
  javascript: { className: 'text-(--file-icon-yellow)', icon: BrandJavascript },
  json: { className: 'text-(--file-icon-yellow)', codicon: 'json' },
  markdown: { className: 'text-(--file-icon-blue)', codicon: 'markdown' },
  npm: { className: 'text-(--ui-red)', icon: BrandNpm },
  python: { className: 'text-(--file-icon-blue)', icon: BrandPython },
  react: { className: 'text-(--ui-cyan)', icon: BrandReact },
  shell: { className: 'text-(--ui-green)', codicon: 'terminal' },
  typescript: { className: 'text-(--file-icon-blue)', icon: BrandTypescript }
} satisfies Record<string, FileGlyph>

type FileKind = keyof typeof GLYPHS

const BY_NAME = new Map<string, FileKind>([
  ['.bashrc', 'shell'],
  ['.env', 'config'],
  ['.gitignore', 'config'],
  ['.zshrc', 'shell'],
  ['cmakelists.txt', 'config'],
  ['dockerfile', 'docker'],
  ['makefile', 'config'],
  ['package-lock.json', 'npm'],
  ['package.json', 'npm']
])

const BY_EXTENSION = new Map<string, FileKind>([
  ['c', 'c'],
  ['h', 'c'],
  ['cc', 'cpp'],
  ['cpp', 'cpp'],
  ['cxx', 'cpp'],
  ['hpp', 'cpp'],
  ['hxx', 'cpp'],
  ['py', 'python'],
  ['pyi', 'python'],
  ['pyw', 'python'],
  ['ts', 'typescript'],
  ['mts', 'typescript'],
  ['cts', 'typescript'],
  ['js', 'javascript'],
  ['mjs', 'javascript'],
  ['cjs', 'javascript'],
  ['tsx', 'react'],
  ['jsx', 'react'],
  ['json', 'json'],
  ['jsonc', 'json'],
  ['json5', 'json'],
  ['yaml', 'config'],
  ['yml', 'config'],
  ['toml', 'config'],
  ['ini', 'config'],
  ['cfg', 'config'],
  ['conf', 'config'],
  ['md', 'markdown'],
  ['markdown', 'markdown'],
  ['mdx', 'markdown'],
  ['sh', 'shell'],
  ['bash', 'shell'],
  ['zsh', 'shell'],
  ['fish', 'shell'],
  ['ps1', 'shell'],
  ['bat', 'shell'],
  ['cmd', 'shell']
])

export function FileTreeIcon({ name }: { name: string }) {
  const basename = name.replace(/\\/g, '/').split('/').pop()!.toLowerCase()
  const family = basename.startsWith('dockerfile.') ? 'docker' : basename.startsWith('.env.') ? 'config' : undefined
  const dot = basename.lastIndexOf('.')
  const kind = BY_NAME.get(basename) ?? family ?? (dot > 0 ? BY_EXTENSION.get(basename.slice(dot + 1)) : undefined)

  if (!kind) {
    return <Codicon name="file" size="0.875rem" />
  }

  const glyph: FileGlyph = GLYPHS[kind]
  const Icon = glyph.icon

  return Icon ? (
    <Icon aria-hidden="true" className={cn('size-3.5 shrink-0', glyph.className)} />
  ) : (
    <Codicon className={glyph.className} name={glyph.codicon!} size="0.875rem" />
  )
}
