import { Codicon } from '@/components/ui/codicon'
import {
  BrandCpp,
  BrandCSharp,
  BrandDocker,
  BrandGit,
  BrandGolang,
  BrandJavascript,
  BrandKotlin,
  BrandNodejs,
  BrandNpm,
  BrandPython,
  BrandReact,
  BrandSass,
  BrandSvelte,
  BrandSwift,
  BrandVite,
  FileTypeCss,
  FileTypeCsv,
  FileTypeDoc,
  FileTypeHtml,
  FileTypePdf,
  FileTypePng,
  FileTypeRs,
  FileTypeSql,
  FileTypeSvg,
  FileTypeTs,
  FileTypeTxt,
  FileTypeVue,
  FileTypeXls,
  FileTypeXml,
  type IconComponent,
  LetterC,
  LetterD,
  LetterJ,
  LetterL,
  LetterP,
  LetterR,
  LetterS
} from '@/lib/icons'
import { cn } from '@/lib/utils'

/**
 * File-type marks for the Files tree: Material Icon Theme's per-language hue
 * assignments with Bearded Icons' saturation, tuned for dark surfaces.
 *
 * Every glyph names a `--fi-*` CSS variable rather than a literal color. The
 * palette lives in `file-icon.css` with a light block and a `:root.dark`
 * override, so both appearances stay legible without touching global tokens.
 */
interface FileGlyph {
  codicon?: string
  icon?: IconComponent
}

const GLYPHS = {
  archive: { codicon: 'file-zip' },
  batch: { codicon: 'terminal-cmd' },
  binary: { codicon: 'file-binary' },
  c: { icon: LetterC },
  cpp: { icon: BrandCpp },
  csharp: { icon: BrandCSharp },
  css: { icon: FileTypeCss },
  csv: { icon: FileTypeCsv },
  dart: { icon: LetterD },
  database: { codicon: 'database' },
  docker: { icon: BrandDocker },
  doc: { icon: FileTypeDoc },
  env: { codicon: 'key' },
  font: { codicon: 'text-size' },
  git: { icon: BrandGit },
  go: { icon: BrandGolang },
  html: { icon: FileTypeHtml },
  image: { icon: FileTypePng },
  ini: { codicon: 'settings-gear' },
  java: { icon: LetterJ },
  javascript: { icon: BrandJavascript },
  json: { codicon: 'json' },
  kotlin: { icon: BrandKotlin },
  less: { codicon: 'symbol-color' },
  license: { codicon: 'law' },
  lock: { codicon: 'lock' },
  log: { codicon: 'output' },
  lua: { icon: LetterL },
  make: { codicon: 'tools' },
  markdown: { codicon: 'markdown' },
  node: { icon: BrandNodejs },
  notebook: { codicon: 'notebook' },
  npm: { icon: BrandNpm },
  pdf: { icon: FileTypePdf },
  perl: { icon: LetterP },
  php: { codicon: 'code' },
  powershell: { codicon: 'terminal-powershell' },
  python: { icon: BrandPython },
  r: { codicon: 'graph' },
  react: { icon: BrandReact },
  ruby: { icon: LetterR },
  rust: { icon: FileTypeRs },
  scala: { icon: LetterS },
  scss: { icon: BrandSass },
  shell: { codicon: 'terminal' },
  sheets: { icon: FileTypeXls },
  slides: { codicon: 'preview' },
  sql: { icon: FileTypeSql },
  svelte: { icon: BrandSvelte },
  svg: { icon: FileTypeSvg },
  swift: { icon: BrandSwift },
  text: { icon: FileTypeTxt },
  toml: { codicon: 'settings-gear' },
  typescript: { icon: FileTypeTs },
  vite: { icon: BrandVite },
  vue: { icon: FileTypeVue },
  xml: { icon: FileTypeXml },
  yaml: { codicon: 'settings-gear' }
} satisfies Record<string, FileGlyph>

/** A known file type: the key selects both the glyph and its `--fi-*` color. */
export type FileKind = keyof typeof GLYPHS

// Exact filenames and config families win over the extension table, so
// `package.json` reads as npm and `Dockerfile.dev` keeps the Docker mark.
const BY_NAME = new Map<string, FileKind>([
  ['.babelrc', 'javascript'],
  ['.bashprofile', 'shell'],
  ['.bashrc', 'shell'],
  ['.dockerignore', 'docker'],
  ['.editorconfig', 'ini'],
  ['.env', 'env'],
  ['.eslintrc', 'javascript'],
  ['.gitattributes', 'git'],
  ['.gitignore', 'git'],
  ['.gitmodules', 'git'],
  ['.npmrc', 'npm'],
  ['.nvmrc', 'node'],
  ['.prettierrc', 'javascript'],
  ['.profile', 'shell'],
  ['.zshrc', 'shell'],
  ['changelog', 'markdown'],
  ['cmakelists.txt', 'make'],
  ['codeowners', 'git'],
  ['contributing', 'markdown'],
  ['docker-compose.yml', 'docker'],
  ['dockerfile', 'docker'],
  ['gemfile', 'ruby'],
  ['license', 'license'],
  ['makefile', 'make'],
  ['package-lock.json', 'npm'],
  ['package.json', 'npm'],
  ['pipfile', 'python'],
  ['pyproject.toml', 'python'],
  ['readme', 'markdown'],
  ['requirements.txt', 'python'],
  ['tsconfig.json', 'typescript'],
  ['vite.config.js', 'vite'],
  ['vite.config.ts', 'vite'],
  ['webpack.config.js', 'node'],
  ['webpack.config.ts', 'node'],
  ['yarn.lock', 'npm']
])

const BY_EXTENSION = new Map<string, FileKind>([
  ['7z', 'archive'],
  ['aac', 'font'],
  ['bash', 'shell'],
  ['bat', 'batch'],
  ['bin', 'binary'],
  ['bmp', 'image'],
  ['bz2', 'archive'],
  ['c', 'c'],
  ['cc', 'cpp'],
  ['cfg', 'ini'],
  ['cjs', 'javascript'],
  ['cmd', 'batch'],
  ['conf', 'ini'],
  ['cpp', 'cpp'],
  ['cs', 'csharp'],
  ['csh', 'shell'],
  ['css', 'css'],
  ['csv', 'csv'],
  ['cts', 'typescript'],
  ['cxx', 'cpp'],
  ['dart', 'dart'],
  ['db', 'database'],
  ['diff', 'git'],
  ['dll', 'binary'],
  ['doc', 'doc'],
  ['docx', 'doc'],
  ['dylib', 'binary'],
  ['env', 'env'],
  ['exe', 'binary'],
  ['fish', 'shell'],
  ['gif', 'image'],
  ['go', 'go'],
  ['gz', 'archive'],
  ['h', 'c'],
  ['hpp', 'cpp'],
  ['htm', 'html'],
  ['html', 'html'],
  ['hxx', 'cpp'],
  ['ico', 'image'],
  ['ini', 'ini'],
  ['ipynb', 'notebook'],
  ['jar', 'archive'],
  ['java', 'java'],
  ['jpeg', 'image'],
  ['jpg', 'image'],
  ['js', 'javascript'],
  ['json', 'json'],
  ['json5', 'json'],
  ['jsonc', 'json'],
  ['jsx', 'react'],
  ['kt', 'kotlin'],
  ['kts', 'kotlin'],
  ['less', 'less'],
  ['lock', 'lock'],
  ['log', 'log'],
  ['lua', 'lua'],
  ['md', 'markdown'],
  ['mdx', 'markdown'],
  ['mjs', 'javascript'],
  ['mk', 'make'],
  ['mkv', 'slides'],
  ['mov', 'slides'],
  ['mp3', 'font'],
  ['mp4', 'slides'],
  ['mts', 'typescript'],
  ['nix', 'text'],
  ['otf', 'font'],
  ['patch', 'git'],
  ['pdf', 'pdf'],
  ['php', 'php'],
  ['pl', 'perl'],
  ['png', 'image'],
  ['ppt', 'slides'],
  ['pptx', 'slides'],
  ['ps1', 'powershell'],
  ['psd', 'image'],
  ['py', 'python'],
  ['pyi', 'python'],
  ['pyw', 'python'],
  ['r', 'r'],
  ['rar', 'archive'],
  ['rb', 'ruby'],
  ['rs', 'rust'],
  ['rst', 'markdown'],
  ['scala', 'scala'],
  ['scss', 'scss'],
  ['sh', 'shell'],
  ['so', 'binary'],
  ['sql', 'sql'],
  ['sqlite', 'database'],
  ['svelte', 'svelte'],
  ['svg', 'svg'],
  ['swift', 'swift'],
  ['tar', 'archive'],
  ['tex', 'text'],
  ['tgz', 'archive'],
  ['toml', 'toml'],
  ['ts', 'typescript'],
  ['tsv', 'csv'],
  ['tsx', 'react'],
  ['ttf', 'font'],
  ['txt', 'text'],
  ['vue', 'vue'],
  ['wav', 'font'],
  ['webm', 'slides'],
  ['webp', 'image'],
  ['woff', 'font'],
  ['woff2', 'font'],
  ['xls', 'sheets'],
  ['xlsx', 'sheets'],
  ['xml', 'xml'],
  ['yaml', 'yaml'],
  ['yml', 'yaml'],
  ['zip', 'archive'],
  ['zsh', 'shell']
])

/** Lowercased basename of a tree row's name or path (`C:\a\B.ts` → `b.ts`). */
export function fileBasename(name: string): string {
  return name.replace(/\\/g, '/').split('/').pop()!.toLowerCase()
}

/**
 * The type a row's filename resolves to: an exact name/config family first,
 * then the extension. `undefined` means "unrecognized — keep the neutral file
 * glyph", which is also the safe answer for prototype-shaped names.
 */
export function fileKindForName(name: string): FileKind | undefined {
  const basename = fileBasename(name)
  const exact = BY_NAME.get(basename) ?? BY_NAME.get(basename.replace(/\.[^.]+$/, ''))

  if (exact) {
    return exact
  }

  if (basename.startsWith('dockerfile.')) {
    return 'docker'
  }

  if (basename.startsWith('.env.')) {
    return 'env'
  }

  const dot = basename.lastIndexOf('.')

  return dot > 0 ? BY_EXTENSION.get(basename.slice(dot + 1)) : undefined
}

export function FileTreeIcon({ name }: { name: string }) {
  const kind = fileKindForName(name)

  if (!kind) {
    return <Codicon name="file" size="0.875rem" />
  }

  const glyph: FileGlyph = GLYPHS[kind]
  const Icon = glyph.icon

  return Icon ? (
    <Icon aria-hidden="true" className={cn('size-3.5 shrink-0')} style={{ color: `var(--fi-${kind})` }} />
  ) : (
    <Codicon className="shrink-0" name={glyph.codicon!} size="0.875rem" style={{ color: `var(--fi-${kind})` }} />
  )
}
