import { execFileSync } from 'node:child_process'
import path from 'node:path'

import { expect, test } from '@playwright/test'

import { writeEnvFile, writeMockProviderConfig } from '../../../tests-js/scripts/mock-provider-config'
import { startMockServer } from '../../../tests-js/scripts/mock-server'

import { buildAppEnv, createSandbox, launchDesktop, waitForAppReady } from './fixtures'
import { taskAnalysisReply } from './task-analysis-fixture'

const SOURCE = 'Verify the release on the target device before acceptance.'

for (const mode of ['local', 'messaging']) {
  test(`returns from a task to its canonical ${mode} compressed source`, async () => {
    test.setTimeout(180000)
    const sandbox = createSandbox('task-canonical-source')
    const mock = await startMockServer({ replyForPrompt: taskAnalysisReply })
    const root = path.resolve(import.meta.dirname, '../../..')
    const python = process.env.HERMES_DESKTOP_PYTHON

    if (!python) { throw new Error('Set HERMES_DESKTOP_PYTHON to the verified interpreter') }
    writeMockProviderConfig(sandbox.hermesHome, mock.url)
    writeEnvFile(sandbox.hermesHome, 'e2e-mock-key', mock.url)
    execFileSync(python, ['-B', 'tests/gateway/task_source_navigation_loopback.py', '--seed', sandbox.hermesHome, mode, mock.url, SOURCE], {
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
      await panel.getByLabel('Task name', { exact: true }).fill('Canonical source acceptance')
      await panel.getByLabel('Session link', { exact: true }).fill('@session:default/compressed-tip')
      await panel.getByRole('button', { name: 'Save task', exact: true }).click()
      await expect(panel.getByText('Target-device acceptance is still required.')).toBeVisible({ timeout: 60000 })
      await expect(panel.getByRole('button', { name: 'Mark accepted', exact: true })).toBeVisible()
      await panel.getByRole('button', { name: 'Open source session', exact: true }).click()
      await expect(panel.getByRole('button', { name: 'Open source session', exact: true })).toBeEnabled({ timeout: 30000 })
      await expect(panel.getByRole('alert')).toHaveCount(0)
      await expect(page.locator('[data-session-anchor] [data-slot="aui_directive-text"]').filter({ hasText: SOURCE })).toBeVisible()
      await expect(panel.getByText('compressed-tip', { exact: true })).toBeVisible()
      await page.screenshot({ path: test.info().outputPath('canonical-source-open.png') })
    } finally {
      await app?.close().catch(() => undefined)
      await mock.close()
      sandbox.cleanup()
    }
  })
}
