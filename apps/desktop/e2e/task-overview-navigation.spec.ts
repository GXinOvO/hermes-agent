import { execFileSync } from 'node:child_process'
import path from 'node:path'

import { expect, test } from '@playwright/test'

import { writeEnvFile, writeMockProviderConfig } from '../../../tests-js/scripts/mock-provider-config'
import { startMockServer } from '../../../tests-js/scripts/mock-server'

import { buildAppEnv, createSandbox, launchDesktop, waitForAppReady } from './fixtures'
import { stopSandboxGatewayDaemons } from './run-tmp-reaper'
import { taskAnalysisReply } from './task-analysis-fixture'

test('captures a real source, reopens it after navigating away, and refuses a restarted owner', async () => {
  test.setTimeout(180_000)
  const sandbox = createSandbox('exact-task-source')
  const mock = await startMockServer({ replyForPrompt: taskAnalysisReply })
  const root = path.resolve(import.meta.dirname, '../../..')
  const python = process.env.HERMES_DESKTOP_PYTHON

  if (!python) { throw new Error('Set HERMES_DESKTOP_PYTHON to this checkout’s verified interpreter') }
  writeMockProviderConfig(sandbox.hermesHome, mock.url)
  writeEnvFile(sandbox.hermesHome)
  execFileSync(python, ['-B', '-c', [
    'from hermes_state import SessionDB',
    'from pathlib import Path',
    'import sys',
    'db = SessionDB(db_path=Path(sys.argv[1]) / "state.db")',
    'for sid, text in [("exact-source-native", "Source conversation preserved"), ("other-source-native", "Other conversation marker")]:',
    '    db.create_session(sid, source="desktop", model="mock-model", model_config={"gateway_runtime": {"provider": "custom", "base_url": sys.argv[2] + "/v1", "api_mode": "chat_completions"}})',
    '    db.append_message(sid, role="user", content=text)',
    '    db.append_message(sid, role="assistant", content="Stored response for " + sid)',
    'db.close()'
  ].join('\n'), sandbox.hermesHome, mock.url], {
    cwd: root, env: { ...process.env, HERMES_HOME: sandbox.hermesHome, HOME: sandbox.root, PYTHONPATH: root }
  })
  let app: Awaited<ReturnType<typeof launchDesktop>>['app'] | undefined

  try {
    const launched = await launchDesktop(buildAppEnv(sandbox))
    app = launched.app
    const page = launched.page
    await waitForAppReady({ ...launched, mock, mockUrl: mock.url, sandbox, cleanup: async () => {} })
    const sidebar = page.locator('[data-slot="sidebar"]').first()
    await sidebar.getByText('Source conversation preserved', { exact: true }).click()
    await expect(page.locator('[data-session-anchor="workspace"] [data-slot="aui_directive-text"]').filter({ hasText: 'Source conversation preserved' })).toBeVisible({ timeout: 30_000 })
    await page.getByRole('button', { name: 'Task follow-up', exact: true }).click()
    const panel = page.getByRole('region', { name: 'Task follow-up', exact: true })
    await panel.getByRole('button', { name: 'New task', exact: true }).click()
    await panel.getByLabel('Task name', { exact: true }).fill('Exact source matter')
    await panel.getByRole('button', { name: 'Link current conversation', exact: true }).click()
    await panel.getByRole('button', { name: 'Save task', exact: true }).click()
    await expect(panel.getByText('exact-source-native', { exact: true })).toBeVisible()
    await expect(panel.getByRole('button', { name: 'Open source session', exact: true })).toBeEnabled()
    await sidebar.getByText('Other conversation marker', { exact: true }).click()
    await expect(page.locator('[data-session-anchor="workspace"] [data-slot="aui_directive-text"]').filter({ hasText: 'Other conversation marker' })).toBeVisible({ timeout: 30_000 })
    await panel.getByRole('button', { name: 'Open source session', exact: true }).click()
    await expect(panel.getByRole('button', { name: 'Open source session', exact: true })).toBeEnabled({ timeout: 30_000 })
    await expect(panel.getByRole('alert')).toHaveCount(0)
    await expect(page.locator('[data-session-anchor] [data-slot="aui_directive-text"]').filter({ hasText: 'Source conversation preserved' })).toBeVisible()
    await app.close()
    stopSandboxGatewayDaemons(sandbox.root, sandbox.hermesHome)
    const relaunched = await launchDesktop(buildAppEnv(sandbox))
    app = relaunched.app
    const restarted = relaunched.page
    await restarted.getByRole('button', { name: 'Task follow-up', exact: true }).and(restarted.locator('[data-size="icon-titlebar"]')).click()
    const retained = restarted.getByRole('region', { name: 'Task follow-up', exact: true })
    await expect(retained.getByRole('button', { name: /Exact source matter/ })).toBeVisible({ timeout: 30_000 })
    await expect(retained.getByRole('button', { name: 'Open source session', exact: true })).toBeDisabled()
    await expect(retained.getByText('Source activity: Source unavailable', { exact: true })).toBeVisible()
    await expect(retained.getByRole('alert')).toContainText('session_owner_changed')
  } finally {
    await app?.close().catch(() => undefined)
    await mock.close()
    sandbox.cleanup()
  }
})
