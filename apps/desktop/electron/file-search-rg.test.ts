import fs from 'node:fs'
import path from 'node:path'

import { describe, expect, it } from 'vitest'

import { resolveNativeSearchRg } from './file-search-rg'

describe('native search executable resolution', () => {
  it('resolves and executes the installed or platform rg without installing', async () => {
    const python = process.env.HERMES_TEST_PYTHON || process.env.HERMES_PYTHON
    expect(python).toBeTruthy()

    const binary = await resolveNativeSearchRg(
      { command: python! },
      path.resolve('../..'),
      new AbortController().signal
    )

    expect(path.isAbsolute(binary)).toBe(true)
    expect(fs.statSync(binary).isFile()).toBe(true)
  })

  it('does not swallow cancellation during resolution', async () => {
    const python = process.env.HERMES_TEST_PYTHON || process.env.HERMES_PYTHON
    const controller = new AbortController()
    controller.abort()
    await expect(
      resolveNativeSearchRg({ command: python! }, path.resolve('../..'), controller.signal)
    ).rejects.toMatchObject({ name: 'AbortError' })
  })
})
