import { expect, test } from '@playwright/test'

import { buildAppEnv, launchDesktop, setupMockBackend, waitForAppReady } from './fixtures'

const TEST_TITLE = 'E2E task overview'

test.describe('built-in task overview', () => {
  test('migrates existing tasks without allowing the legacy writer to discard source witnesses', async () => {
    const fixture = await setupMockBackend()

    try {
      await waitForAppReady(fixture)
      const page = fixture.page
      await page.evaluate(() => { window.location.hash = '#/settings?tab=config%3Aappearance&page=window-layout' })
      const toggle = page.getByRole('switch', { name: 'Task follow-up', exact: true })
      await expect(toggle).toBeChecked()
      await toggle.click()
      await expect(page.getByRole('region', { name: 'Task follow-up' })).toHaveCount(0)
      await page.evaluate(() => new Promise<void>((resolve, reject) => {
        const request = indexedDB.deleteDatabase('hermes.plugin.task-overview.followup.v1')
        request.onsuccess = () => resolve()
        request.onerror = () => reject(request.error)
      }))

      const original = {
        id: 'migration-task', revision: 4,
        task: {
          version: 1, id: 'migration-task', title: 'Migrated source task', description: 'Original description', notes: 'Original notes',
          primary: {
            connectionId: 'source', profile: 'default', targetProfile: 'worker', storedId: 'source-session', mode: 'remote',
            storeInstanceId: 'source-store',
            expectedOwner: { instanceId: 'source-runtime', profileId: '/profiles/worker', authorityEpoch: 3, storeInstanceId: 'source-store' }
          },
          related: [], nextSteps: ['Keep the source witness'], status: 'todo', priority: 'high',
          createdAt: 1_700_000_000_000, updatedAt: 1_700_000_000_000, resolution: { state: 'open' }
        }
      }

      await page.evaluate(row => new Promise<void>((resolve, reject) => {
        const request = indexedDB.open('hermes.plugin.task-overview.followup.v1', 1)
        request.onupgradeneeded = () => request.result.createObjectStore('tasks', { keyPath: 'id' })
        request.onerror = () => reject(request.error)

        request.onsuccess = () => {
          const db = request.result
          db.onversionchange = () => db.close()
          const transaction = db.transaction('tasks', 'readwrite')
          transaction.objectStore('tasks').add(row)
          transaction.oncomplete = () => resolve()
          transaction.onabort = () => reject(transaction.error)
        }
      }), original)
      await toggle.click()
      await page.evaluate(() => { window.location.hash = '#/' })
      await page.getByRole('button', { name: 'Task follow-up', exact: true }).click()
      await expect(page.getByRole('button', { name: /Migrated source task/ })).toBeVisible()
      await page.getByRole('button', { name: 'Mark accepted', exact: true }).click()
      await page.getByLabel('Acceptance note').fill('Migration verified')
      await page.getByRole('button', { name: 'Confirm accepted', exact: true }).click()
      await expect(page.getByRole('button', { name: 'Completed · 1', exact: true })).toBeVisible()

      const stored = await page.evaluate(() => new Promise<{ revision: number; task: typeof original.task }>((resolve, reject) => {
        const request = indexedDB.open('hermes.plugin.task-overview.followup.v1')
        request.onerror = () => reject(request.error)

        request.onsuccess = () => {
          const db = request.result
          const transaction = db.transaction('tasks', 'readonly')
          const get = transaction.objectStore('tasks').get('migration-task')
          get.onsuccess = () => resolve(get.result)
          get.onerror = () => reject(get.error)
          transaction.oncomplete = () => db.close()
        }
      }))

      expect(stored.task.primary).toEqual(original.task.primary)
      expect(stored.task.description).toBe(original.task.description)
      expect(stored.task.notes).toBe(original.task.notes)
      expect(stored.task.nextSteps).toEqual(original.task.nextSteps)
      expect(stored.revision).toBe(5)
      expect(await page.evaluate(() => new Promise<string>((resolve, reject) => {
        const request = indexedDB.open('hermes.plugin.task-overview.followup.v1', 1)
        request.onerror = () => resolve(request.error?.name || '')

        request.onsuccess = () => { request.result.close(); reject(new Error('Legacy writer can still open the shared task store')) }
      }))).toBe('VersionError')
    } finally {
      await fixture.app.close().catch(() => undefined)
      await fixture.mock.close()
      fixture.sandbox.cleanup()
    }
  })

  test('creates, accepts, re-tracks, and keeps a task after restart', async () => {
    const fixture = await setupMockBackend()
    const { sandbox } = fixture
    let app: Awaited<ReturnType<typeof launchDesktop>>['app'] | undefined = fixture.app

    try {
      await waitForAppReady(fixture)
      const page = fixture.page

      await expect(page.getByRole('button', { name: 'Task follow-up' })).toBeVisible({ timeout: 60_000 })
      await page.getByRole('button', { name: 'Task follow-up' }).click()
      await expect(page.getByRole('region', { name: 'Task follow-up' })).toBeVisible()

      await page.getByRole('button', { name: 'New task' }).click()
      await page.getByLabel('Task name').fill(TEST_TITLE)
      await page.getByLabel('Description', { exact: true }).fill('Business objective')
      await page.getByLabel('Notes', { exact: true }).fill('Evidence to verify')
      await page.getByLabel('Remaining steps').fill('Verify persistence')
      await page.getByRole('combobox', { name: 'Status', exact: true }).click()
      await page.getByRole('option', { name: 'Waiting', exact: true }).click()
      await page.getByRole('combobox', { name: 'Priority', exact: true }).click()
      await page.getByRole('option', { name: 'Urgent', exact: true }).click()
      await page.getByRole('button', { name: 'Save task' }).click()
      await expect(page.getByRole('button', { name: /E2E task overview/ })).toBeVisible()
      await expect(page.getByText('Status: Waiting · Priority: Urgent', { exact: true })).toBeVisible()
      await page.getByRole('button', { name: 'Edit task', exact: true }).click()
      await page.getByLabel('Remaining steps').fill('Verify persistence\nReview delivery')
      await page.getByRole('button', { name: 'Save task', exact: true }).click()
      const taskPane = page.getByRole('region', { name: 'Task follow-up' })
      await expect(taskPane.getByRole('listitem')).toHaveText(['Verify persistence', 'Review delivery'])
      await page.getByLabel('Search tasks', { exact: true }).fill('Evidence to verify')
      await expect(page.getByRole('button', { name: /E2E task overview/ })).toBeVisible()
      await page.getByLabel('Search tasks', { exact: true }).fill('no-such-matter')
      await expect(page.getByRole('button', { name: /E2E task overview/ })).toHaveCount(0)
      await page.getByLabel('Search tasks', { exact: true }).fill('')
      await page.getByRole('combobox', { name: 'Sort tasks', exact: true }).click()
      await page.getByRole('option', { name: 'Priority', exact: true }).click()
      await page.getByRole('combobox', { name: 'Filter by status', exact: true }).click()
      await page.getByRole('option', { name: 'To do', exact: true }).click()
      await expect(page.getByRole('button', { name: /E2E task overview/ })).toHaveCount(0)
      await page.getByRole('combobox', { name: 'Filter by status', exact: true }).click()
      await page.getByRole('option', { name: 'All statuses', exact: true }).click()

      await page.getByRole('button', { name: 'Mark accepted' }).click()
      await page.getByLabel('Acceptance note').fill('Verified in Electron')
      await page.getByRole('button', { name: 'Confirm accepted' }).click()
      await expect(page.getByRole('button', { name: /Completed/ })).toContainText('1')

      await page.getByRole('button', { name: 'Completed · 1' }).click()
      await page.getByRole('button', { name: 'Re-track' }).click()
      await expect(page.getByRole('button', { name: 'Open · 1' })).toBeVisible()

      await page.evaluate(() => { window.location.hash = '#/settings?tab=config%3Aappearance&page=window-layout' })
      const toggle = page.getByRole('switch', { name: 'Task follow-up', exact: true })
      await expect(toggle).toBeChecked()
      await toggle.click()
      await expect(page.getByRole('button', { name: 'Task follow-up', exact: true })).toHaveCount(0)

      await app.close()
      const relaunched = await launchDesktop(buildAppEnv(sandbox))
      app = relaunched.app
      const restarted = relaunched.page
      await restarted.evaluate(() => { window.location.hash = '#/settings?tab=config%3Aappearance&page=window-layout' })
      const restoredToggle = restarted.getByRole('switch', { name: 'Task follow-up', exact: true })
      await expect(restoredToggle).not.toBeChecked()
      await expect(restarted.getByRole('button', { name: 'Task follow-up', exact: true })).toHaveCount(0)
      await restoredToggle.click()
      await restarted.evaluate(() => { window.location.hash = '#/' })
      await restarted.getByRole('button', { name: 'Task follow-up', exact: true }).and(restarted.locator('[data-size="icon-titlebar"]')).click()
      await expect(restarted.getByRole('button', { name: /E2E task overview/ })).toBeVisible({ timeout: 30_000 })
      await expect(restarted.getByRole('region', { name: 'Task follow-up' }).getByRole('listitem')).toHaveText(['Verify persistence', 'Review delivery'])
      await expect(restarted.getByText('Business objective', { exact: true })).toBeVisible()
      await expect(restarted.getByText('Evidence to verify', { exact: true })).toBeVisible()
      await expect(restarted.getByText('Status: To do · Priority: Urgent', { exact: true })).toBeVisible()
    } finally {
      await app?.close().catch(() => undefined)
      await fixture.mock.close()
      sandbox.cleanup()
    }
  })
})
