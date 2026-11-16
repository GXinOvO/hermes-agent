/**
 * Mock-model reply for the task-analysis one-shot. The backend sends one
 * stateless user message whose content is the analysis input JSON
 * (`{ title, messages, limitations }`); the mock must answer with exactly the
 * six analysis fields and evidence quotes copied from the supplied rows.
 *
 * The prompt can arrive wrapped in framing text, so extract the outermost JSON
 * object instead of requiring `prompt` to be pure JSON. Every returned quote is
 * a prefix of a real supplied row, which `validate_output` accepts as an exact
 * source quote.
 */
export function taskAnalysisReply(prompt: string): string {
  const rows = extractRows(prompt)
  const user = rows.find(row => row.role === 'user' && typeof row.content === 'string' && row.content.trim())

  if (!user || typeof user.row_id !== 'number') {
    return JSON.stringify(fallback())
  }

  const evidence = [{ row_id: user.row_id, quote: user.content.slice(0, 120) }]

  return JSON.stringify({
    status: 'waiting',
    goal: { text: 'Verify the release', evidence },
    completed: [],
    remaining: [{ text: 'Target-device acceptance is still required.', evidence }],
    needs_user: [{ text: 'Please verify the release on the target device.', evidence }],
    limitations: []
  })
}

interface Row { row_id: number; role: string; content: string }

function extractRows(prompt: string): Row[] {
  const parsed = extractJson(prompt)
  const messages = parsed && (parsed as { messages?: unknown }).messages

  return Array.isArray(messages) ? messages as Row[] : []
}

function extractJson(prompt: string): unknown {
  try {
    return JSON.parse(prompt)
  } catch {
    // Ignore surrounding framing: scan for the outermost balanced JSON object.
  }

  const start = prompt.indexOf('{')

  if (start < 0) { return null }

  let depth = 0
  let inString = false

  for (let i = start; i < prompt.length; i++) {
    const ch = prompt[i]

    if (ch === '"' && prompt[i - 1] !== '\\') { inString = !inString }
    if (inString) { continue }
    if (ch === '{') { depth++ }
    if (ch === '}') {
      depth--
      if (depth === 0) {
        try {
          return JSON.parse(prompt.slice(start, i + 1))
        } catch {
          return null
        }
      }
    }
  }

  return null
}

function fallback() {
  const evidence = [{ row_id: 1, quote: 'Verify the release on the target device before acceptance.' }]

  return {
    status: 'waiting', goal: null, completed: [],
    remaining: [{ text: 'Target-device acceptance is still required.', evidence }],
    needs_user: [{ text: 'Please verify the release on the target device.', evidence }],
    limitations: []
  }
}
