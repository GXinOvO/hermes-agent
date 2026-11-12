'use strict'

const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const crypto = require('node:crypto')
const { spawnSync } = require('node:child_process')
const { createRequire } = require('node:module')

const desktop = process.env.FOLLOWUP_DESKTOP || path.resolve(__dirname, '../../../apps/desktop')
const repo = path.resolve(desktop, '../..')
const plugin = path.resolve(__dirname, '../desktop/plugin.js')
const scratch = process.env.FOLLOWUP_E2E_SCRATCH || os.tmpdir()
const electron = process.env.FOLLOWUP_ELECTRON || createRequire(path.join(desktop, 'package.json'))('electron')
const libraries = process.env.FOLLOWUP_ELECTRON_LIBS || process.env.LD_LIBRARY_PATH
const python = process.env.FOLLOWUP_DESKTOP_PYTHON || process.env.HERMES_PYTHON || 'python3'
const display = process.env.FOLLOWUP_E2E_DISPLAY || ':0'
const { _electron } = createRequire(path.join(desktop, 'package.json'))('@playwright/test')
const root = fs.mkdtempSync(path.join(scratch, 'fu-e2e-'))
const hermesHome = path.join(root, 'hermes-home')
const mockDir = path.join(root, 'mock')
for (const dir of ['home', 'hermes-home/desktop-plugins/task-overview', 'electron-user-data', 'runtime', 'config', 'cache', 'data', 'mock']) fs.mkdirSync(path.join(root, dir), { recursive: true, mode: 0o700 })
fs.copyFileSync(plugin, path.join(hermesHome, 'desktop-plugins/task-overview/plugin.js'))
const shortTmp = path.join(scratch, 'fu-tmp')
fs.mkdirSync(shortTmp, { recursive: true, mode: 0o700 })

/**
 * Rebuild the mock inference server + provider-config writers from the real
 * checkout sources with the already-installed esbuild, into THIS run's sandbox.
 * No package is installed: `zod` is declared by tests-js/package.json but its
 * node_modules link is not present in this checkout, so the pinned copy already
 * in the pnpm store is supplied through esbuild's `nodePaths`.
 */
async function buildMockModules() {
  const build = createRequire(path.join(desktop, 'package.json'))('esbuild')
  const store = path.join(repo, 'node_modules/.pnpm')
  if (!fs.existsSync(store)) throw new Error(`No pnpm store to resolve mock deps from: ${store}`)
  const zodPaths = fs.readdirSync(store)
    .filter(name => /^zod@/.test(name))
    .sort()
    .map(name => path.join(store, name, 'node_modules'))
  const nodePaths = [...zodPaths, path.join(repo, 'node_modules')]
  const entries = [
    ['tests-js/scripts/mock-server.ts', 'mock-server.mjs'],
    ['tests-js/scripts/mock-provider-config.ts', 'mock-provider-config.mjs']
  ]
  const built = []
  for (const [source, output] of entries) {
    const entry = path.join(repo, source)
    if (!fs.existsSync(entry)) throw new Error(`Mock source missing from checkout: ${entry}`)
    const outfile = path.join(mockDir, output)
    await build.build({ entryPoints: [entry], outfile, bundle: true, platform: 'node', format: 'esm', target: 'node22', nodePaths, logLevel: 'warning', absWorkingDir: repo })
    built.push({ source: entry, output: outfile, sha256: sha(outfile), bytes: fs.statSync(outfile).size })
  }
  return { esbuild: build.version, built }
}

/** Fail loudly, before launching, when the chosen interpreter lacks the backend stack. */
function assertBackendPython() {
  const required = ['openai', 'httpx', 'rich', 'ruamel.yaml', 'fastapi', 'uvicorn', 'pydantic', 'dotenv', 'fire', 'truststore', 'requests', 'tomli_w', 'socksio']
  const probe = `import importlib, sys
mods = ${JSON.stringify(required)}
missing = []
for name in mods:
    try: importlib.import_module(name)
    except Exception as error: missing.append(f"{name}: {type(error).__name__}")
if missing:
    sys.exit("missing: " + ", ".join(missing))
print(sys.version.split()[0])`
  const run = spawnSync(python, ['-c', probe], { encoding: 'utf8', env: { PATH: process.env.PATH, HOME: path.join(root, 'home'), PYTHONNOUSERSITE: '1', PYTHONDONTWRITEBYTECODE: '1' }, cwd: repo })
  if (run.status !== 0) throw new Error(`FOLLOWUP_DESKTOP_PYTHON is not a usable desktop interpreter (${python}): ${(run.stderr || run.stdout || '').trim() || `exit ${run.status}`}`)
  return { python, version: (run.stdout || '').trim(), required }
}

const env = {
  PATH: process.env.PATH, LANG: 'C.UTF-8', DISPLAY: display, ...(libraries ? { LD_LIBRARY_PATH: libraries } : {}),
  HOME: path.join(root, 'home'), TMPDIR: shortTmp, XDG_RUNTIME_DIR: path.join(root, 'runtime'),
  XDG_CONFIG_HOME: path.join(root, 'config'), XDG_CACHE_HOME: path.join(root, 'cache'), XDG_DATA_HOME: path.join(root, 'data'),
  HERMES_HOME: hermesHome, HERMES_DESKTOP_USER_DATA_DIR: path.join(root, 'electron-user-data'),
  HERMES_DESKTOP_ISOLATED_BACKEND: '1', HERMES_DESKTOP_IGNORE_EXISTING: '1', HERMES_DESKTOP_APP_NAME: 'HermesFollowupE2E',
  HERMES_DESKTOP_SKIP_QUIT_CONFIRM: '1', HERMES_DESKTOP_SKIP_PROTOCOL_REGISTRATION: '1', HERMES_DESKTOP_HERMES_ROOT: repo,
  HERMES_DESKTOP_PYTHON: python, HERMES_DISABLE_LAZY_INSTALLS: '1', PYTHONDONTWRITEBYTECODE: '1',
  HERMES_E2E_RUN_TMP: root, NO_PROXY: '*', no_proxy: '*'
}
const assert = (condition, message) => { if (!condition) throw new Error(message) }
const sha = file => crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex')
const cases = []
const record = (name, detail) => { cases.push({ name, detail }); console.log('PASS', name, JSON.stringify(detail ?? null)) }
let app, first, second, mock

async function launch() {
  app = await _electron.launch({ executablePath: electron, args: [desktop, '--disable-gpu', '--no-sandbox'], cwd: root, env, timeout: 30000 })
  const page = await app.firstWindow({ timeout: 30000 })
  await openPane(page)
  return page
}

async function openPane(page) {
  await page.getByRole('button', { name: /Tracked ·/ }).click({ timeout: 120000 })
  await page.getByRole('button', { name: 'New item', exact: true }).waitFor({ state: 'visible', timeout: 30000 })
}

async function rows(page) {
  return page.evaluate(async () => {
    const db = await new Promise((resolve, reject) => {
      const request = indexedDB.open('hermes.plugin.task-overview.followup.v1', 1)
      request.onsuccess = () => resolve(request.result)
      request.onerror = () => reject(request.error)
    })
    try {
      return await new Promise((resolve, reject) => {
        const tx = db.transaction('tasks', 'readonly')
        const req = tx.objectStore('tasks').getAll()
        req.onsuccess = () => resolve(req.result)
        req.onerror = () => reject(req.error)
      })
    } finally { db.close() }
  })
}

async function waitForText(page, text) {
  await page.getByRole('heading', { name: text, exact: true }).waitFor({ state: 'visible', timeout: 30000 })
}

function ownedProcesses() {
  const result = []
  for (const name of fs.readdirSync('/proc')) {
    const pid = Number(name)
    if (!Number.isInteger(pid) || pid <= 1 || pid === process.pid) continue
    try {
      const envs = fs.readFileSync(`/proc/${pid}/environ`).toString().split('\0')
      if (!envs.includes(`HERMES_HOME=${hermesHome}`) || !envs.includes(`HERMES_E2E_RUN_TMP=${root}`)) continue
      const stat = fs.readFileSync(`/proc/${pid}/stat`, 'utf8')
      const start = stat.slice(stat.lastIndexOf(')') + 2).split(' ')[19]
      result.push({ pid, start })
    } catch {}
  }
  return result
}
function stillOwned({pid, start}) {
  try {
    const stat = fs.readFileSync(`/proc/${pid}/stat`, 'utf8')
    const fields = stat.slice(stat.lastIndexOf(')') + 2).split(' ')
    const envs = fs.readFileSync(`/proc/${pid}/environ`).toString().split('\0')
    return fields[0] !== 'Z' && fields[19] === start && envs.includes(`HERMES_HOME=${hermesHome}`) && envs.includes(`HERMES_E2E_RUN_TMP=${root}`)
  } catch { return false }
}
async function cleanup() {
  if (app) { await app.close().catch(() => {}); app = null }
  const pids = ownedProcesses()
  for (const proc of pids) if (stillOwned(proc)) { try { process.kill(proc.pid, 'SIGTERM') } catch {} }
  const deadline = Date.now() + 8000
  while (Date.now() < deadline && pids.some(stillOwned)) await new Promise(resolve => setTimeout(resolve, 100))
  for (const proc of pids) if (stillOwned(proc)) { try { process.kill(proc.pid, 'SIGKILL') } catch {} }
  const still = pids.filter(stillOwned)
  if (mock) { await mock.close(); mock = null }
  console.log('CLEANUP', JSON.stringify({ candidates: pids.length, remaining: still.map(item => item.pid), root }))
  if (still.length) process.exitCode = 1
}

;(async () => {
  let verdict = 'FAIL'
  try {
    const mockBuild = await buildMockModules()
    const backend = assertBackendPython()
    const { startMockServer } = await import(path.join(mockDir, 'mock-server.mjs'))
    const { writeMockProviderConfig, writeEnvFile } = await import(path.join(mockDir, 'mock-provider-config.mjs'))
    mock = await startMockServer()
    writeMockProviderConfig(hermesHome, mock.url)
    writeEnvFile(hermesHome, 'e2e-mock-key', mock.url)
    console.log('PRECONDITIONS', JSON.stringify({ backend, mock: mockBuild, libraries: libraries || null }))
    console.log('SANDBOX', JSON.stringify({ root, pluginSha256: sha(plugin), desktopMainSha256: sha(path.join(desktop, 'dist/electron-main.mjs')), display }))
    first = await launch()
    assert((await rows(first)).length === 0, 'Fresh sandbox already has task records')
    await first.getByRole('button', { name: 'New item', exact: true }).click()
    const form = first.getByRole('form', { name: 'New item' })
    await form.getByLabel('Item title').fill('Follow-up sandbox task')
    await form.getByLabel('Description').fill('Original description')
    await form.getByLabel('Notes').fill('Persist this note')
    await form.getByLabel('Remaining steps').fill('Review the result')
    await form.getByRole('button', { name: 'Waiting', exact: true }).click()
    await form.getByRole('button', { name: 'High', exact: true }).click()
    await form.getByRole('button', { name: 'Save item', exact: true }).click()
    await waitForText(first, 'Follow-up sandbox task')
    let saved = await rows(first)
    assert(saved.length === 1 && saved[0].revision === 1 && saved[0].task.status === 'waiting' && saved[0].task.priority === 'high' && saved[0].task.notes === 'Persist this note' && saved[0].task.primary === null, 'Created task did not persist all fields')
    record('native creation and storage readback', { revision: saved[0].revision, status: saved[0].task.status, priority: saved[0].task.priority })

    const opened = app.waitForEvent('window', { timeout: 30000 })
    const result = await first.evaluate(() => window.hermesDesktop.openWindow())
    assert(result?.ok === true, `Native second-window request failed: ${JSON.stringify(result)}`)
    second = await opened
    await openPane(second)
    await waitForText(second, 'Follow-up sandbox task')
    assert((await rows(second))[0]?.task.notes === 'Persist this note', 'Second window did not read first window data')
    record('native second window readback', { url: second.url() })

    await first.getByRole('button', { name: 'Edit item', exact: true }).click()
    await second.getByRole('button', { name: 'Edit item', exact: true }).click()
    const editA = first.getByRole('form', { name: 'Edit item' })
    const editB = second.getByRole('form', { name: 'Edit item' })
    await editA.getByLabel('Description').fill('Window one wins')
    await editB.getByLabel('Description').fill('Window two stale edit')
    await editA.getByRole('button', { name: 'Save changes' }).click()
    await first.getByText('Window one wins', { exact: true }).waitFor({ timeout: 30000 })
    await editB.getByRole('button', { name: 'Save changes' }).click()
    await second.getByRole('alert').filter({ hasText: /revision conflict/i }).waitFor({ timeout: 30000 })
    saved = await rows(second)
    assert(saved.length === 1 && saved[0].revision === 2 && saved[0].task.description === 'Window one wins', 'Stale write changed stored winner')
    assert(await editB.getByLabel('Description').inputValue() === 'Window two stale edit', 'Conflicting draft was lost')
    record('native two-window stale write refusal', { revision: saved[0].revision, winner: saved[0].task.description, staleDraftRetained: true })
    await editB.getByRole('button', { name: 'Cancel', exact: true }).click()

    await first.getByRole('button', { name: 'Edit item', exact: true }).click()
    const failedEdit = first.getByRole('form', { name: 'Edit item' })
    await failedEdit.getByLabel('Description').fill('Retry after aborted transaction')
    await first.evaluate(() => {
      const original = IDBDatabase.prototype.transaction
      window.__followupAbort = { original, count: 0 }
      IDBDatabase.prototype.transaction = function (stores, mode, ...rest) {
        const tx = original.call(this, stores, mode, ...rest)
        if (this.name === 'hermes.plugin.task-overview.followup.v1' && mode === 'readwrite') {
          window.__followupAbort.count++
          queueMicrotask(() => tx.abort())
        }
        return tx
      }
    })
    await failedEdit.getByRole('button', { name: 'Save changes' }).click()
    await first.getByRole('alert').filter({ hasText: 'Follow-up storage transaction failed' }).waitFor({ timeout: 30000 })
    const abortCount = await first.evaluate(() => window.__followupAbort.count)
    saved = await rows(second)
    assert(abortCount === 1 && saved.length === 1 && saved[0].revision === 2 && saved[0].task.description === 'Window one wins', 'Aborted transaction changed stored data or was not reached')
    assert(await failedEdit.getByLabel('Description').inputValue() === 'Retry after aborted transaction', 'Failed write lost the draft')
    await first.evaluate(() => { IDBDatabase.prototype.transaction = window.__followupAbort.original })
    await failedEdit.getByRole('button', { name: 'Save changes' }).click()
    await first.getByText('Retry after aborted transaction', { exact: true }).waitFor({ timeout: 30000 })
    saved = await rows(first)
    assert(saved.length === 1 && saved[0].revision === 3 && saved[0].task.description === 'Retry after aborted transaction', 'Retry did not persist the draft')
    record('native aborted transaction preserves data and supports retry', { abortCount, beforeRevision: 2, afterRevision: saved[0].revision })
    await app.close(); app = null

    first = await launch()
    await waitForText(first, 'Follow-up sandbox task')
    saved = await rows(first)
    assert(saved.length === 1 && saved[0].revision === 3 && saved[0].task.description === 'Retry after aborted transaction' && saved[0].task.status === 'waiting' && saved[0].task.priority === 'high' && saved[0].task.notes === 'Persist this note', 'Restart lost persisted task fields')
    record('native application restart persistence', { revision: saved[0].revision, description: saved[0].task.description, status: saved[0].task.status, priority: saved[0].task.priority })

    const composer = first.locator('[data-slot="composer-root"] [contenteditable="true"]').filter({ visible: true }).first()
    await composer.fill('Follow-up isolated session visibility check')
    await composer.press('Enter')
    await first.getByText('Hello from the mock inference server!', { exact: false }).first().waitFor({ state: 'visible', timeout: 120000 })
    await first.getByRole('button', { name: 'Track focused session', exact: true }).click()
    const tracked = first.getByRole('form', { name: 'Tracking session' })
    await tracked.getByText('Source unverified; opening from the panel is unavailable', { exact: true }).waitFor({ timeout: 30000 })
    await tracked.getByLabel('Item title').fill('Isolated session clue')
    await tracked.getByRole('button', { name: 'Save item', exact: true }).click()
    await waitForText(first, 'Isolated session clue')
    await first.getByText('Source unverified; opening from the panel is unavailable', { exact: true }).waitFor({ timeout: 30000 })
    assert(await first.getByRole('button', { name: 'Open main session', exact: true }).isDisabled(), 'Unverified session could be opened')
    saved = await rows(first)
    const clue = saved.find(row => row.task.title === 'Isolated session clue')
    assert(clue?.task.primary?.unverified === true && typeof clue.task.primary.storedId === 'string' && clue.task.primary.storedId.length > 0 && !('targetProfile' in clue.task.primary), 'Session clue lacks unverified original identity or invented a backend')
    assert(clue.task.status === 'todo' && saved.find(row => row.task.title === 'Follow-up sandbox task')?.task.status === 'waiting', 'Tracking a session changed a task status')
    record('native unverified focused-session clue', { storedId: clue.task.primary.storedId, openingDisabled: true, originalTaskStatus: 'waiting' })
    await app.close(); app = null
    first = await launch()
    saved = await rows(first)
    assert(saved.some(row => row.task.title === 'Isolated session clue' && row.task.primary?.storedId === clue.task.primary.storedId && row.task.primary?.unverified === true), 'Unverified session clue did not survive app restart')
    record('native unverified clue survives restart', { storedId: clue.task.primary.storedId })

    const archive = await first.evaluate(async id => {
      const api = window.hermesDesktop.api
      const path = `/api/sessions/${encodeURIComponent(id)}`
      const list = archived => `/api/sessions?limit=100&offset=0&min_messages=0&archived=${archived}&order=recent&profile=default`
      const before = await api({ path: list('exclude') })
      if (!before.sessions?.some(row => row.id === id)) throw new Error('Fixture session was not present in the normal list')
      const snapshot = await api({ path: `${path}/mutation-snapshot?profile=default` })
      if (!snapshot.exists || !Number.isSafeInteger(snapshot.runtime_revision) || !Number.isSafeInteger(snapshot.runtime_generation)) throw new Error('Fixture session mutation snapshot unavailable')
      const receipt = await api({ path, method: 'PATCH', body: { archived: true, pinned: false, profile: 'default', request_id: crypto.randomUUID(), expected_revision: snapshot.runtime_revision, expected_generation: snapshot.runtime_generation } })
      const normal = await api({ path: list('exclude') })
      const archived = await api({ path: list('only') })
      return { receipt, absentFromNormal: !normal.sessions?.some(row => row.id === id), presentInArchive: archived.sessions?.some(row => row.id === id) }
    }, clue.task.primary.storedId)
    assert(archive.receipt?.ok === true && archive.absentFromNormal && archive.presentInArchive, `Fixture session was not verifiably archived: ${JSON.stringify(archive)}`)
    await first.getByRole('button', { name: /Isolated session clue/ }).click()
    await waitForText(first, 'Isolated session clue')
    assert(await first.getByRole('button', { name: 'Open main session', exact: true }).isDisabled(), 'Archived unverified session could be opened')
    saved = await rows(first)
    assert(saved.some(row => row.task.title === 'Isolated session clue' && row.task.status === 'todo' && row.task.primary?.storedId === clue.task.primary.storedId), 'Archiving a session changed or removed its task')
    record('archived session leaves normal list but task remains', { storedId: clue.task.primary.storedId, absentFromNormal: true, presentInArchive: true, taskStatus: 'todo' })
    await app.close(); app = null
    first = await launch()
    saved = await rows(first)
    assert(saved.some(row => row.task.title === 'Isolated session clue' && row.task.status === 'todo' && row.task.primary?.storedId === clue.task.primary.storedId), 'Task disappeared after restarting with archived session')
    record('task remains after archived-session restart', { storedId: clue.task.primary.storedId, taskStatus: 'todo' })

    const panel = first.locator('.followup-view')
    const tabs = panel.locator('.tabs')
    const taskRows = panel.locator('.list > button')
    await tabs.getByRole('button', { name: /^Waiting · 1$/ }).click()
    await waitForText(first, 'Follow-up sandbox task')
    assert(await taskRows.count() === 1 && (await taskRows.first().innerText()).includes('Follow-up sandbox task'), 'Waiting filter returned the wrong tasks')
    await tabs.getByRole('button', { name: /^To do · 1$/ }).click()
    await waitForText(first, 'Isolated session clue')
    assert(await taskRows.count() === 1 && (await taskRows.first().innerText()).includes('Isolated session clue'), 'To do filter returned the wrong tasks')
    await tabs.getByRole('button', { name: /^Unfinished · 2$/ }).click()
    const search = panel.getByRole('textbox', { name: 'Search items or sessions' })
    await search.fill('Retry after aborted transaction')
    await waitForText(first, 'Follow-up sandbox task')
    assert(await taskRows.count() === 1 && (await taskRows.first().innerText()).includes('Follow-up sandbox task'), 'Description search returned the wrong tasks')
    await search.fill('')
    await panel.locator('[aria-label="Sort"]').getByRole('button', { name: 'Priority', exact: true }).click()
    assert(await taskRows.count() === 2 && (await taskRows.first().innerText()).includes('Follow-up sandbox task'), 'Priority sorting did not put the high-priority task first')
    await panel.locator('[aria-label="Priority filter"]').getByRole('button', { name: 'High', exact: true }).click()
    assert(await taskRows.count() === 1 && (await taskRows.first().innerText()).includes('Follow-up sandbox task'), 'Priority filtering returned the wrong tasks')
    await panel.locator('[aria-label="Priority filter"]').getByRole('button', { name: 'Any', exact: true }).click()
    record('native filters, search and priority sorting', { waiting: 1, todo: 1, descriptionSearch: 1, highPriority: 1 })

    await taskRows.filter({ hasText: 'Follow-up sandbox task' }).click()
    await panel.getByRole('button', { name: 'Mark accepted', exact: true }).click()
    const confirmAcceptance = panel.getByRole('button', { name: 'Confirm acceptance', exact: true })
    assert(await confirmAcceptance.isDisabled(), 'Acceptance without a note was enabled')
    saved = await rows(first)
    assert(saved.find(row => row.task.title === 'Follow-up sandbox task')?.task.resolution.state === 'open', 'Opening acceptance silently completed the task')
    await panel.getByLabel('Acceptance note').fill('Reviewed in the sandbox')
    await confirmAcceptance.click()
    await tabs.getByRole('button', { name: /^Completed · 1$/ }).click()
    await waitForText(first, 'Follow-up sandbox task')
    saved = await rows(first)
    const accepted = saved.find(row => row.task.title === 'Follow-up sandbox task')
    assert(accepted?.task.status === 'done' && accepted.task.resolution.state === 'accepted' && accepted.task.resolution.note === 'Reviewed in the sandbox', 'Explicit acceptance was not persisted')
    record('native explicit acceptance', { status: accepted.task.status, note: accepted.task.resolution.note })

    await tabs.getByRole('button', { name: /^All · 2$/ }).click()
    await taskRows.filter({ hasText: 'Isolated session clue' }).click()
    await panel.getByRole('button', { name: 'Delete item', exact: true }).click()
    await panel.getByRole('alertdialog', { name: 'Delete item' }).getByRole('button', { name: 'Cancel', exact: true }).click()
    assert((await rows(first)).some(row => row.task.title === 'Isolated session clue'), 'Cancelled deletion removed the task')
    await panel.getByRole('button', { name: 'Delete item', exact: true }).click()
    await panel.getByRole('alertdialog', { name: 'Delete item' }).getByRole('button', { name: 'Confirm deletion', exact: true }).click()
    await first.waitForFunction(() => !document.querySelector('.followup-view .list')?.textContent?.includes('Isolated session clue'), null, { timeout: 30000 })
    saved = await rows(first)
    assert(saved.length === 1 && saved[0].task.title === 'Follow-up sandbox task' && saved[0].task.resolution.state === 'accepted', 'Deletion changed the wrong task')
    const archivedStillPresent = await first.evaluate(async id => {
      const data = await window.hermesDesktop.api({ path: '/api/sessions?limit=100&offset=0&min_messages=0&archived=only&order=recent&profile=default' })
      return data.sessions?.some(row => row.id === id) === true
    }, clue.task.primary.storedId)
    assert(archivedStillPresent, 'Deleting the task deleted the archived session')
    record('native deletion preserves related session and other task', { remainingTasks: saved.length, archivedSessionPreserved: true })
    verdict = 'PASS'
  } catch (error) {
    console.error('FAIL', error.stack || String(error))
    for (const [name, page] of [['first', first], ['second', second]]) {
      try { if (page && !page.isClosed()) await page.screenshot({ path: path.join(root, `${name}-failure.png`) }) } catch {}
    }
    process.exitCode = 1
  } finally {
    await cleanup()
    console.log('RESULT', JSON.stringify({ status: verdict, cases, root, pluginSha256: sha(plugin) }))
  }
})()
