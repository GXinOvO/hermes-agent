import type { FileSearchFrame, FileSearchPayload, FileSearchRequest } from '@hermes/shared/file-search'

/** Preserve the query's captured file owner. Session workspace selection belongs
 * in the URL: the backend's existing profile/session boundary validates it. */
export function fileSearchApiRequest(
  owner: Pick<FileSearchPayload, 'connectionId' | 'profile' | 'sessionId'>,
  request: FileSearchRequest,
  run: { signal: AbortSignal; onFrame: (frame: FileSearchFrame) => void }
) {
  const session = owner.sessionId ? `?session_id=${encodeURIComponent(owner.sessionId)}` : ''

  return {
    connectionId: owner.connectionId,
    profile: owner.profile,
    path: `/api/fs/search${session}`,
    method: 'POST',
    body: request,
    timeoutMs: (request.timeoutMs ?? 15000) + 5000,
    searchStream: run
  }
}
