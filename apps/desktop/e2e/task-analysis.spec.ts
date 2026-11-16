import { execFileSync } from 'node:child_process'
import path from 'node:path'

import { expect, test } from '@playwright/test'

import { writeEnvFile, writeMockProviderConfig } from '../../../tests-js/scripts/mock-provider-config'
import { startMockServer } from '../../../tests-js/scripts/mock-server'

import { buildAppEnv, createSandbox, launchDesktop, waitForAppReady } from './fixtures'
import { taskAnalysisReply } from './task-analysis-fixture'

const SOURCE = 'Verify the release on the target device before acceptance.'

for (const compressed of [false, true]) {
test(`creates from a ${compressed ? 'compressed tip' : 'source'}, displays grounded analysis, and preserves it across restart`, async () => {
  test.setTimeout(180000)
  const sandbox = createSandbox('task-analysis')
  const mock = await startMockServer({ replyForPrompt: taskAnalysisReply })
  const root = path.resolve(import.meta.dirname, '../../..')
  const python = process.env.HERMES_DESKTOP_PYTHON

  if (!python) { throw new Error('Set HERMES_DESKTOP_PYTHON to the verified interpreter') }
  writeMockProviderConfig(sandbox.hermesHome, mock.url)
  writeEnvFile(sandbox.hermesHome)
  execFileSync(python, ['-B', '-c', [
    'from hermes_state import SessionDB', 'from pathlib import Path', 'import sys',
    'db = SessionDB(db_path=Path(sys.argv[1]) / "state.db")',
    'route = {"gateway_runtime": {"provider": "custom", "base_url": sys.argv[3] + "/v1", "api_mode": "openai"}}',
    'db.create_session("analysis-source", source="desktop", model="mock-model", model_config=route)',
    'db.append_message("analysis-source", role="user", content=sys.argv[2])',
    'db.append_message("analysis-source", role="assistant", content="Implementation ready; target-device acceptance remains pending.")',
    'if sys.argv[4] == "true":',
    '    assert db.try_acquire_compression_lock("analysis-source", "e2e-analysis")',
    '    db.publish_compression_child(parent_session_id="analysis-source", child_session_id="analysis-tip", source="desktop", model="mock-model", model_config=route, messages=[{"role": "user", "content": "Continue the original request; target-device acceptance remains pending."}], compression_lock_holder="e2e-analysis")',
    '    db.release_compression_lock("analysis-source", "e2e-analysis")',
    'db.close()'
  ].join('\n'), sandbox.hermesHome, SOURCE, mock.url, String(compressed)], {
    cwd: root, env: { ...process.env, HERMES_HOME: sandbox.hermesHome, HOME: sandbox.root, PYTHONPATH: root }
  })
  let app: Awaited<ReturnType<typeof launchDesktop>>['app'] | undefined

  try {
    const launched = await launchDesktop(buildAppEnv(sandbox))
    app = launched.app
    const { page } = launched
    await waitForAppReady({ ...launched, mock, mockUrl: mock.url, sandbox, cleanup: async () => {} })
    await page.getByRole('button', { name: 'Task follow-up', exact: true }).and(page.locator('[data-size="icon-titlebar"]')).click()
    const panel = page.getByRole('region', { name: 'Task follow-up', exact: true })
    await panel.getByRole('button', { name: 'New task', exact: true }).click()
    await panel.getByLabel('Task name', { exact: true }).fill('Release acceptance')
    await panel.getByLabel('Session link', { exact: true }).fill(`@session:default/${compressed ? 'analysis-tip' : 'analysis-source'}`)
    await expect(panel.getByLabel('Description', { exact: true })).toHaveCount(0)
    await expect(panel.getByLabel('Remaining steps', { exact: true })).toHaveCount(0)
    await panel.getByRole('button', { name: 'Save task', exact: true }).click()
    await expect(panel.getByText('Target-device acceptance is still required.')).toBeVisible({ timeout: 60000 })
    await expect(panel.getByText('Please verify the release on the target device.')).toBeVisible()
    const quote = panel.locator('details').filter({ hasText: 'Target-device acceptance is still required.' })
    await quote.locator('summary').click()
    await expect(quote.getByText(SOURCE, { exact: true })).toBeVisible()
    await expect(panel.getByRole('button', { name: 'Mark accepted', exact: true })).toBeVisible()
    const count = mock.receivedPrompts.length
    await page.screenshot({ path: test.info().outputPath('analysis-populated.png') })
    await app.close()
    const relaunched = await launchDesktop(buildAppEnv(sandbox))
    app = relaunched.app
    await waitForAppReady({ ...relaunched, mock, mockUrl: mock.url, sandbox, cleanup: async () => {} })
    await relaunched.page.getByRole('button', { name: 'Task follow-up', exact: true }).and(relaunched.page.locator('[data-size="icon-titlebar"]')).click()
    const restored = relaunched.page.getByRole('region', { name: 'Task follow-up', exact: true })
    await expect(restored.getByText('Target-device acceptance is still required.')).toBeVisible()
    expect(mock.receivedPrompts.length).toBe(count)
    await restored.getByRole('button', { name: 'Open source session', exact: true }).click()
    await expect(relaunched.page.locator('[data-session-anchor] [data-slot="aui_directive-text"]').filter({ hasText: SOURCE })).toBeVisible()
  } finally {
    await app?.close().catch(() => undefined)
    await mock.close()
    sandbox.cleanup()
  }
})
}
