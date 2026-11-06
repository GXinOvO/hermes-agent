import { test } from 'node:test'
import assert from 'node:assert/strict'
import { deriveOverview, loadOverview } from '../src/core.mjs'

const real = '::task{title="real" result="real result"}'
const fake = '::task{title="example" result="fake result" tests="passed" tests_evidence="example.log"}'
for (const [name, code] of [
  ['shorter nested fence', `\`\`\`\`text\n\`\`\`\n${fake}\n\`\`\`\n\`\`\`\``],
  ['fake closing fence with text', `\`\`\`text\n\`\`\`not a close\n${fake}\n\`\`\``],
  ['tilde fence with shorter close', `~~~~text\n~~~\n${fake}\n~~~~`],
  ['four-space indented code', `    ${fake}`],
  ['tab-indented code', `\t${fake}`],
  ['quoted fenced block', `> \`\`\`text\n> ${fake}\n> \`\`\``],
  ['list-contained fenced block', `- \`\`\`text\n  ${fake}\n  \`\`\``],
  ['lazy blockquote continuation', `> example\n${fake}`],
  ['HTML pre block', `<pre>\n${fake}\n</pre>`],
  ['multiline inline code', `\`example\n${fake}\n\``],
  ['GFM table row', `| field |\n| --- |\n${fake}`],
  ['math block', `$$\n${fake}\n$$`],
]) {
  test(`code context cannot override registration: ${name}`, () => {
    const value = deriveOverview({ messages: [{ role: 'assistant', content: `${real}\n\n${code}` }] })
    assert.equal(value.title, 'real')
    assert.equal(value.result, 'real result')
    assert(value.stages.every(stage => stage.status === 'unknown'))
  })
}

test('directive string values remain literal while checking Markdown context', () => {
  const value = deriveOverview({ messages: [{ role: 'assistant', content: '::task{title="literal `code` and [link](url)" result="kept"}' }] })
  assert.equal(value.title, 'literal `code` and [link](url)')
  assert.equal(value.result, 'kept')
})

test('normal registration after a valid longer closing fence remains accepted', () => {
  const value = deriveOverview({ messages: [{ role: 'assistant', content: `\`\`\`text\n${fake}\n\`\`\`\`\n\n${real}` }] })
  assert.equal(value.title, 'real')
  assert.equal(value.result, 'real result')
})

const scope = { storedId: 'stored', runtimeId: 'runtime', owner: { connectionId: 'owner', profile: 'default' } }
const route = { ...scope.owner, targetProfile: 'default' }
const envelope = fields => ({ type: 'plugin', output: JSON.stringify({ version: 2, ok: true, requested_id: 'stored', resolved_id: 'tip', snapshot_scope: 'persisted_display', todo_state: null, fetchedAt: Date.now(), complete: true, count: 0, messages: [], ...fields }) })

test('verification uses only the command-bound workspace snapshot without a second ambient RPC', async () => {
  const calls = []
  const verification = { status: 'passed', session_id: 'tip', root: '/session-project', evidence: { scope: 'targeted' } }
  const host = { profileRoutes: async () => [route], requestProfile: async (r, method, args) => {
    calls.push({ method, args })
    return method === 'command.dispatch' ? envelope({ verification, verification_cwd: '/session-project/subdir', verification_error: null }) : { verification: { status: 'passed', root: '/ambient-wrong-project' } }
  } }
  const value = await loadOverview(host, scope)
  assert.deepEqual(calls.map(call => call.method), ['command.dispatch'])
  assert.equal(value.verification.root, '/session-project')
  assert.equal(value.verificationCwd, '/session-project/subdir')
})

test('missing bound workspace never falls back to ambient verification', async () => {
  const calls = []
  const host = { profileRoutes: async () => [route], requestProfile: async (r, method) => {
    calls.push(method)
    return method === 'command.dispatch' ? envelope({ verification: null, verification_cwd: null, verification_error: 'Session workspace unavailable' }) : { verification: { status: 'passed', root: '/ambient' } }
  } }
  const value = await loadOverview(host, scope)
  assert.equal(value.verification, null)
  assert.match(value.verificationError, /workspace unavailable/)
  assert.deepEqual(calls, ['command.dispatch'])
})
