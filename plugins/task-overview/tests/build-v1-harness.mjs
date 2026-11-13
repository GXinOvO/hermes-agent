import path from 'node:path'
import { readdir } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const desktop = process.env.TASK_OVERVIEW_DESKTOP
if (!desktop) throw new Error('TASK_OVERVIEW_DESKTOP is required')
const { build } = createRequire(path.join(desktop, 'package.json'))('esbuild')
const names = await readdir(path.join(root, 'tests'))
let built = 0
for (const source of names.filter(name => name.endsWith('.harness.jsx'))) {
  const name = source.slice(0, -'.harness.jsx'.length)
  for (const [variant, sdk] of [['harness', 'sdk-stub.jsx'], ['real-controls', 'sdk-real-controls.jsx']]) {
    if (!names.includes(`${name}.${variant}.html`)) continue
    await build({
      entryPoints: [path.join(root, 'tests', source)],
      outfile: path.join(root, `tests/${name}.${variant}.bundle.mjs`),
      bundle: true, format: 'esm', platform: 'browser', jsx: 'automatic',
      alias: { '@hermes/plugin-sdk': path.join(root, 'tests', sdk), '@': path.join(desktop, 'src') },
      nodePaths: [path.join(desktop, 'node_modules')], logLevel: 'warning'
    })
    built++
  }
}
console.log(`Built ${built} browser harness bundles`)