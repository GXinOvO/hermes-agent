import { execFileSync } from 'node:child_process'
import path from 'node:path'

import { expect, test } from '@playwright/test'

import { writeEnvFile, writeMockProviderConfig } from '../../../tests-js/scripts/mock-provider-config'
import { startMockServer } from '../../../tests-js/scripts/mock-server'

import { buildAppEnv, createSandbox, launchDesktop, waitForAppReady } from './fixtures'
import { stopSandboxGatewayDaemons } from './run-tmp-reaper'

test('built-in session overview reads native requirements, checklist and evidence without a plugin', async () => {
  test.setTimeout(180_000)
  const sandbox = createSandbox('core-session-review')
  const mock = await startMockServer()
  const root = path.resolve(import.meta.dirname, '../../..')
  const python = process.env.HERMES_DESKTOP_PYTHON

  if (!python) { throw new Error('Set HERMES_DESKTOP_PYTHON to this checkout’s verified interpreter') }
  writeMockProviderConfig(sandbox.hermesHome, mock.url)
  writeEnvFile(sandbox.hermesHome)
  execFileSync(python, ['-B', '-c', [
    'from hermes_state import SessionDB',
    'from pathlib import Path',
    'import json, sys',
    'db = SessionDB(db_path=Path(sys.argv[1]) / "state.db")',
    'sid = "core-review-native"',
    'db.create_session(sid, source="desktop")',
    'db.set_session_title(sid, "Core overview fixture")',
    'db.append_message(sid, role="user", content="Deliver the original feature")',
    'db.append_message(sid, role="assistant", content="", tool_calls=[{"id":"paired-todo","type":"function","function":{"name":"todo_list","arguments":"{}"}}])',
    'db.append_message(sid, role="tool", tool_name="todo_list", tool_call_id="paired-todo", content=json.dumps({"revision":4,"todos":[{"id":"remote","content":"Validate remote adoption","status":"pending"},{"id":"built","content":"Implementation","status":"completed"}]}))',
    'db.append_message(sid, role="assistant", content=\'::task{title="Feature review" result="Implementation recorded" remaining="Publish release" deferred="Mobile extension" decision="Choose remote host" tests="passed" tests_evidence="core-native-receipt.json"}\')',
    'db.append_message(sid, role="user", content="Now verify it on the remote host")',
    'db.append_message(sid, role="assistant", content="Remote adoption still needs verification.")',
    'db.close()'
  ].join('\n'), sandbox.hermesHome], {
    cwd: root, env: { ...process.env, HERMES_HOME: sandbox.hermesHome, HOME: sandbox.root, PYTHONPATH: root }
  })
  let app: Awaited<ReturnType<typeof launchDesktop>>['app'] | undefined

  try {
    const launched = await launchDesktop(buildAppEnv(sandbox))
    app = launched.app
    const page = launched.page
    await waitForAppReady({ ...launched, mock, mockUrl: mock.url, sandbox, cleanup: async () => {} })
    const sidebar = page.locator('[data-slot="sidebar"]').first()
    await sidebar.getByText('Core overview fixture', { exact: true }).click()
    await expect(page.locator('[data-session-anchor="workspace"] [data-slot="aui_directive-text"]').filter({ hasText: 'Now verify it on the remote host' })).toBeVisible({ timeout: 30_000 })
    await page.getByRole('button', { name: 'Task follow-up', exact: true }).and(page.locator('[data-size="icon-titlebar"]')).click()
    await page.getByRole('button', { name: 'Session overview', exact: true }).click()
    const panel = page.getByRole('region', { name: 'Session overview', exact: true })
    await expect(panel.getByRole('heading', { name: 'Feature review', exact: true })).toBeVisible({ timeout: 30_000 })
    await expect(panel.getByText('Deliver the original feature', { exact: true })).toBeVisible()
    await expect(panel.getByText('Now verify it on the remote host', { exact: true })).toBeVisible()
    await expect(panel.getByText('Validate remote adoption', { exact: true }).first()).toBeVisible()
    await expect(panel.getByText('Publish release', { exact: true })).toBeVisible()
    await expect(panel.getByText('Choose remote host', { exact: true })).toBeVisible()
    await expect(panel.getByText('Mobile extension', { exact: true })).toBeVisible()
    await expect(panel.getByText('core-native-receipt.json', { exact: true })).toBeVisible()
    await expect(panel.getByRole('heading', { name: 'Agent-recorded result', exact: true })).toBeVisible()
    await expect(panel.getByText('Implementation recorded', { exact: true })).toBeVisible()
    await expect(panel.getByText('Remote adoption still needs verification.', { exact: true })).toBeVisible()
    await expect(panel.getByText('Record outdated', { exact: true }).first()).toBeVisible()
    await expect(panel.getByRole('button', { name: 'Mark accepted', exact: true })).toHaveCount(0)
    await panel.getByRole('button', { name: 'Refresh overview', exact: true }).click()
    await expect(panel.getByRole('button', { name: 'Refresh overview', exact: true })).toBeEnabled()
    await expect(panel.getByRole('alert')).toHaveCount(0)
    const composer = page.locator('[data-session-anchor="workspace"] [contenteditable="true"]').first()
    const promptsBefore = mock.receivedPrompts.length
    await panel.getByRole('button', { name: 'Update task record', exact: true }).click()
    await expect(composer).toContainText('::task')
    await expect(composer).toContainText('pending')
    expect(mock.receivedPrompts.length).toBe(promptsBefore)
    await page.screenshot({ path: test.info().outputPath('session-review-light.png') })
    await page.setViewportSize({ width: 900, height: 800 })
    await expect(panel.getByText('core-native-receipt.json', { exact: true })).toBeVisible()
    expect(await panel.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true)
    await page.screenshot({ path: test.info().outputPath('session-review-narrow.png') })

    const secondWindow = app.waitForEvent('window')
    await page.evaluate(() => (window as unknown as { hermesDesktop: { openWindow: () => Promise<unknown> } }).hermesDesktop.openWindow())
    const peer = await secondWindow
    await peer.locator('[data-slot="sidebar"]').first().getByText('Core overview fixture', { exact: true }).click()
    await expect(peer.locator('[data-session-anchor="workspace"] [data-slot="aui_directive-text"]').filter({ hasText: 'Now verify it on the remote host' })).toBeVisible({ timeout: 30_000 })
    await peer.getByRole('button', { name: 'Task follow-up', exact: true }).and(peer.locator('[data-size="icon-titlebar"]')).click()
    await peer.getByRole('button', { name: 'Session overview', exact: true }).click()
    const peerPanel = peer.getByRole('region', { name: 'Session overview', exact: true })
    const candidate = panel.getByRole('listitem').filter({ hasText: 'Validate remote adoption' }).getByRole('button', { name: 'Track as matter', exact: true })
    const peerCandidate = peerPanel.getByRole('listitem').filter({ hasText: 'Validate remote adoption' }).getByRole('button', { name: 'Track as matter', exact: true })
    await expect(peerCandidate).toBeEnabled({ timeout: 30_000 })
    await Promise.all([candidate.click(), peerCandidate.click()])
    await expect(panel.getByRole('button', { name: 'Tracked', exact: true })).toHaveCount(1)
    await expect(peerPanel.getByRole('button', { name: 'Tracked', exact: true })).toHaveCount(1)

    const readMatters = async () => page.evaluate(async () => {
      const database = await new Promise<IDBDatabase>((resolve, reject) => {
        const request = indexedDB.open('hermes.plugin.task-overview.followup.v1')

        request.onupgradeneeded = () => { request.transaction?.abort(); reject(new Error('Product task database was not created')) }
        request.onsuccess = () => resolve(request.result)
        request.onerror = () => reject(request.error)
      })

      try {
        return await new Promise<Array<{ task: { primary: Record<string, unknown>; notes: string; nextSteps: string[] } }>>((resolve, reject) => {
          const transaction = database.transaction('tasks', 'readonly')
          const request = transaction.objectStore('tasks').getAll()
          transaction.oncomplete = () => resolve(request.result)
          transaction.onerror = () => reject(transaction.error)
        })
      } finally { database.close() }
    })

    const rows = await readMatters()
    expect(rows).toHaveLength(1)
    expect(rows[0].task.nextSteps).toEqual(['Validate remote adoption'])
    expect(rows[0].task.notes).toBe('Persisted session snapshot.')
    expect(rows[0].task.primary).toMatchObject({ storedId: 'core-review-native', mode: 'local', profile: 'default', targetProfile: 'default' })
    expect(Object.keys(rows[0].task.primary.expectedOwner as object).sort()).toEqual(['authorityEpoch', 'instanceId', 'profileId', 'storeInstanceId'])
    expect(mock.receivedPrompts.length).toBe(promptsBefore)
    await page.getByRole('button', { name: 'Task follow-up', exact: true }).and(page.locator('[aria-pressed]')).click()
    const matters = page.getByRole('region', { name: 'Task follow-up', exact: true })
    await expect(matters.getByRole('button', { name: /Validate remote adoption/ })).toBeVisible()
    await matters.getByRole('button', { name: 'Mark accepted', exact: true }).click()
    await matters.getByLabel('Acceptance note', { exact: true }).fill('Native discovery persistence verified')
    await matters.getByRole('button', { name: 'Confirm accepted', exact: true }).click()
    await matters.getByRole('button', { name: /Completed/ }).click()
    await matters.getByRole('button', { name: 'Re-track', exact: true }).click()
    await expect(matters.getByRole('button', { name: /Validate remote adoption/ })).toBeVisible()
    expect((await readMatters())[0].task.primary).toEqual(rows[0].task.primary)
    await app.close()
    stopSandboxGatewayDaemons(sandbox.root, sandbox.hermesHome)
    const restarted = await launchDesktop(buildAppEnv(sandbox))
    app = restarted.app
    const restored = restarted.page
    await restored.locator('[data-slot="sidebar"]').first().getByText('Core overview fixture', { exact: true }).click()
    await expect(restored.locator('[data-session-anchor="workspace"] [data-slot="aui_directive-text"]').filter({ hasText: 'Now verify it on the remote host' })).toBeVisible({ timeout: 30_000 })
    await restored.getByRole('button', { name: 'Task follow-up', exact: true }).and(restored.locator('[data-size="icon-titlebar"]')).click()
    await restored.getByRole('button', { name: 'Attention', exact: true }).click()
    const attention = restored.getByRole('region', { name: 'Attention', exact: true })
    await expect(attention.getByRole('button', { name: /Validate remote adoption/ })).toBeVisible()
    await expect(attention.getByRole('button', { name: 'Open source session', exact: true })).toBeDisabled()
    await attention.getByRole('combobox', { name: 'Source activity', exact: true }).click()
    await restored.getByRole('option', { name: 'Source unavailable', exact: true }).click()
    await expect(attention.getByRole('button', { name: /Validate remote adoption/ })).toBeVisible()
    await expect(attention.getByText('Source activity: Source unavailable', { exact: true })).toBeVisible()
    expect(mock.receivedPrompts.length).toBe(promptsBefore)
    await restored.screenshot({ path: test.info().outputPath('attention-restarted-owner.png') })
  } finally {
    await app?.close().catch(() => undefined)
    await mock.close()
    sandbox.cleanup()
  }
})
