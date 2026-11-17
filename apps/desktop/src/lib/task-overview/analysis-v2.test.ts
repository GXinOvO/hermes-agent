import { expect, test } from 'vitest'

import { validateTaskAnalysis } from './analysis-model'

const owner = { instance_id: 'runtime', profile_id: '/profiles/work', authority_epoch: 3, store_instance_id: 'store' }

const source = (overrides: Record<string, unknown> = {}) => ({
  id: 's0', profile: 'work', session_id: 'root-a', stored_session_id: 'session-a', resolved_id: 'root-a', owner,
  relation: 'primary', via_source_id: null, via_row_id: null, status: 'read', complete: true, reason: null, ...overrides
})

const analysis = (overrides: Record<string, unknown> = {}) => ({
  version: 2, title: '检查发布', analyzed_at: 1700000000100, conversation_revision: [1], stale: false,
  scope: 'linked_conversations', message_count: 2, analyzed_messages: 2, limitations: [], status: 'waiting',
  complete: true, sources: [source()], goal: { text: '检查发布', evidence: [{ source_id: 's0', row_id: 7, quote: '检查发布' }] },
  completed: [], remaining: [], needs_user: [], ...overrides
})

test.each([
  ['rejects duplicate source ids', { sources: [source(), source({ relation: 'reference' })] }],
  ['rejects an unknown via source', { sources: [source({ via_source_id: 'missing', via_row_id: 2 })] }],
  ['rejects a non-positive via row', { sources: [source({ via_source_id: 's0', via_row_id: 0 })] }],
  ['rejects a missing primary source', { sources: [source({ relation: 'reference' })] }],
  ['rejects evidence that points at an unavailable source', {
    sources: [source(), source({ id: 's1', relation: 'reference', status: 'unavailable', resolved_id: null, owner: null, complete: false, reason: 'not found' })],
    goal: { text: '检查发布', evidence: [{ source_id: 's1', row_id: 7, quote: '伪造依据' }] }
  }],
  ['rejects a read source with spoofed owner fields', {
    sources: [source({ owner: { instance_id: 'runtime', profile_id: '/profiles/work', authority_epoch: 3 } })]
  }],
  ['rejects complete analysis with an unavailable source', {
    sources: [source(), source({ id: 's1', relation: 'reference', status: 'unavailable', resolved_id: null, owner: null, complete: false, reason: 'not found' })]
  }]
])('%s', (_name, overrides) => {
  expect(() => validateTaskAnalysis(analysis(overrides))).toThrow('Invalid task analysis')
})

test('accepts v1 evidence without a source id', () => {
  expect(validateTaskAnalysis({
    version: 1, title: '检查发布', analyzed_at: 1700000000100, conversation_revision: [1], stale: false,
    scope: 'single_conversation', message_count: 1, analyzed_messages: 1, limitations: [], status: 'waiting',
    goal: { text: '检查发布', evidence: [{ row_id: 7, quote: '检查发布' }] }, completed: [], remaining: [], needs_user: []
  })).toMatchObject({ version: 1, scope: 'single_conversation' })
})