import fs from 'node:fs/promises'
import os from 'node:os'
import { spawnSync } from 'node:child_process'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { createRequire } from 'node:module'

const root = path.dirname(fileURLToPath(import.meta.url))
const desktopRoot = process.env.TASK_OVERVIEW_DESKTOP || path.resolve(root, '../../apps/desktop')
const require = createRequire(path.join(desktopRoot, 'package.json'))
const { build } = require('esbuild')
const outfile = path.join(root, 'desktop/plugin.js')
const nodePaths = [path.join(desktopRoot, 'node_modules')]
if (process.argv.includes('--test')) {
  const output = await fs.mkdtemp(path.join(os.tmpdir(), 'task-overview-tests-'))
  try {
    const names = (await fs.readdir(path.join(root, 'tests'))).filter(name => name.endsWith('.test.mjs')).sort()
    if (!names.length) throw Error('No task overview tests found')
    await build({ entryPoints: names.map(name => path.join(root, 'tests', name)), outdir: output, bundle: true, platform: 'node', format: 'esm', outExtension: { '.js': '.mjs' }, nodePaths, logLevel: 'warning' })
    const result = spawnSync(process.execPath, ['--test', ...names.map(name => path.join(output, name))], { stdio: 'inherit' })
    process.exitCode = result.status ?? 1
  } finally {
    await fs.rm(output, { recursive: true, force: true })
  }
} else {
  const notices = await fs.readFile(path.join(root, 'desktop/THIRD_PARTY_NOTICES.txt'), 'utf8')
  if (notices.includes('*/')) throw Error('Invalid notice comment delimiter')
  const result = await build({ banner: { js: `/*!${notices}\n*/` }, absWorkingDir: root, nodePaths, minifySyntax: true, minifyIdentifiers: true, minifyWhitespace: false, entryPoints: [path.join(root, 'src/plugin.jsx')], outfile, bundle: true, format: 'esm', platform: 'browser', target: 'es2022', external: ['@hermes/plugin-sdk', 'react', 'react/jsx-runtime'], metafile: true, jsx: 'automatic', logLevel: 'warning', write: false })
  const imports = Object.values(result.metafile.outputs).flatMap(output => output.imports).map(item => item.path)
  if (imports.some(item => !['@hermes/plugin-sdk', 'react', 'react/jsx-runtime'].includes(item))) throw Error(`Invalid runtime imports: ${imports}`)
  const bytes = Buffer.from(result.outputFiles[0].text.replace(/^\/\/ (?:[^\r\n]*?\/)?node_modules\//gm, '// node_modules/'))
  if (process.argv.includes('--check')) {
    if (!Buffer.from(bytes).equals(await fs.readFile(outfile))) throw Error('Desktop bundle differs from source; run node plugins/task-overview/build.mjs')
  } else {
    await fs.writeFile(outfile, bytes)
  }
  console.log(JSON.stringify({ artifact: outfile, imports: [...new Set(imports)], bytes: bytes.length, checked: process.argv.includes('--check') }))
}
