export function taskAnalysisReply(prompt: string): string {
  const input = JSON.parse(prompt)
  const rows = input.messages as Array<{ row_id: number; role: string; content: string }>
  const user = rows.find(row => row.role === 'user' && row.content.trim())

  if (!user || !Number.isSafeInteger(user.row_id)) { throw new Error('Analysis fixture requires supplied source rows') }
  const evidence = [{ row_id: user.row_id, quote: user.content.slice(0, 120) }]

  return JSON.stringify({ status: 'waiting', goal: { text: 'Verify the release', evidence }, completed: [],
    remaining: [{ text: 'Target-device acceptance is still required.', evidence }],
    needs_user: [{ text: 'Please verify the release on the target device.', evidence }], limitations: [] })
}
