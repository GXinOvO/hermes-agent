import { execFileSync } from 'node:child_process'
import path from 'node:path'

import { expect, test } from '@playwright/test'

import { writeEnvFile, writeMockProviderConfig } from '../../../tests-js/scripts/mock-provider-config'
import { startMockServer } from '../../../tests-js/scripts/mock-server'

import { buildAppEnv, createSandbox, launchDesktop, waitForAppReady } from './fixtures'

const PRIMARY = 'Ship the release; target-device acceptance remains manual.'
const HISTORY = 'Historical release checks: 4 passed; deployment remains cancelled.'

test('automatically follows a read source and opens its evidence without changing acceptance', async () => {
  test.setTimeout(180000)
  const sandbox = createSandbox('task-linked-analysis')
  const mock = await startMockServer({ replyForPrompt: prompt => {
    const input = JSON.parse(prompt)
    const rows = input.messages as Array<{ source_id: string; row_id: number; role: string; content: string }>
    const primary = rows.find(row => row.source_id === 's0' && row.role === 'user')
    const history = rows.find(row => row.source_id !== 's0' && row.content.includes(HISTORY))

    if (!primary || !history) { throw new Error('Linked analysis did not read the native historical source') }
    const evidence = (row: typeof primary) => [{ source_id: row.source_id, row_id: row.row_id, quote: row.content }]

    return JSON.stringify({ status: 'awaiting_acceptance', goal: { text: 'Ship the release', evidence: evidence(primary) },
      completed: [{ text: 'Historical release checks passed.', evidence: evidence(history) }],
      remaining: [{ text: 'Target-device acceptance is still required.', evidence: evidence(primary) }],
      needs_user: [{ text: 'Please verify the release on the target device.', evidence: evidence(primary) }], limitations: [] })
  } })
  const root = path.resolve(import.meta.dirname, '../../..')
  const python = process.env.HERMES_DESKTOP_PYTHON

  if (!python) { throw new Error('Set HERMES_DESKTOP_PYTHON to the verified interpreter') }
  writeMockProviderConfig(sandbox.hermesHome, mock.url)
  writeEnvFile(sandbox.hermesHome, 'e2e-mock-key', mock.url)
  execFileSync(python, ['-B', '-c', [
    'from hermes_state import SessionDB', 'from pathlib import Path', 'import sys,json',
    'db = SessionDB(db_path=Path(sys.argv[1]) / "state.db")',
    'route={"gateway_runtime":{"provider":"custom","base_url":sys.argv[4]+"/v1","api_mode":"openai"}}',
    'for sid in ("linked-main", "linked-history"):',
    '    db.create_session(sid,source="desktop",model="mock-model",model_config=route)',
    'db.append_message("linked-main",role="user",content=sys.argv[2])',
    'db.append_message("linked-history",role="user",content="Run the release checks without deploying")',
    'db.append_message("linked-history",role="tool",content=sys.argv[3],tool_name="terminal",tool_call_id="checks")',
    'db.append_message("linked-main",role="assistant",content="",tool_calls=[{"id":"read-history","type":"function","function":{"name":"session_search","arguments":json.dumps({"session_id":"linked-history"})}}])',
    'db.append_message("linked-main",role="tool",content=json.dumps({"success":True,"mode":"read","session_id":"linked-history","link":"@session:default/linked-history","message_count":2,"truncated":False,"messages":[]}),tool_name="session_search",tool_call_id="read-history")',
    'db.close()'
  ].join('\n'), sandbox.hermesHome, PRIMARY, HISTORY, mock.url], {
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
    await panel.getByLabel('Task name', { exact: true }).fill('Release follow-up')
    await panel.getByLabel('Session link', { exact: true }).fill('@session:default/linked-main')
    await panel.getByRole('button', { name: 'Save task', exact: true }).click()
    await expect(panel.getByText('Historical release checks passed.', { exact: false })).toBeVisible({ timeout: 60000 })
    const claim = panel.locator('details').filter({ hasText: 'Historical release checks passed.' })
    await claim.locator('summary').click()
    await expect(claim.getByText(HISTORY, { exact: true })).toBeVisible()
    await claim.getByRole('button', { name: 'Open source session s1', exact: true }).click()
    await expect(page.locator('[data-session-anchor] [data-slot="aui_directive-text"]').filter({ hasText: 'Run the release checks without deploying' })).toBeVisible({ timeout: 30000 })
    await expect(panel.getByRole('button', { name: 'Mark accepted', exact: true })).toBeVisible()
    await expect(panel.getByRole('alert')).toHaveCount(0)
    const count = mock.receivedPrompts.length
    await page.screenshot({ path: test.info().outputPath('linked-evidence-open.png') })
    await app.close()
    const reopened = await launchDesktop(buildAppEnv(sandbox))
    app = reopened.app
    await waitForAppReady({ ...reopened, mock, mockUrl: mock.url, sandbox, cleanup: async () => {} })
    await reopened.page.getByRole('button', { name: 'Task follow-up', exact: true }).and(reopened.page.locator('[data-size="icon-titlebar"]')).click()
    const retained = reopened.page.getByRole('region', { name: 'Task follow-up', exact: true })
    await expect(retained.getByText('Historical release checks passed.', { exact: false })).toBeVisible()
    expect(mock.receivedPrompts.length).toBe(count)
  } finally {
    await app?.close().catch(() => undefined)
    await mock.close()
    sandbox.cleanup()
  }
})
