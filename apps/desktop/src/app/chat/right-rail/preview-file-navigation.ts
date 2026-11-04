import type { PreviewTarget } from '@/store/preview'

import type { PreviewViewMode } from './preview-file'

interface LocalPreviewModeOptions {
  diff?: string
  language?: string
  navigationPending: boolean
  onSelectRendered?: () => void
  target: PreviewTarget
  userMode: PreviewViewMode | null
}

export function previewLocationUnavailable(
  text: string | undefined,
  truncated: boolean | undefined,
  location: PreviewTarget['location']
) {
  if (!truncated || !location) {
    return false
  }

  const lines = (text ?? '').split(/\r\n?|\n/)

  // Only the last line may be incomplete. String length matches the editor's
  // one-based UTF-16 columns, including its valid end-of-line caret position.
  return (
    location.line > lines.length ||
    (location.line === lines.length && (location.column ?? 1) > lines[lines.length - 1]!.length + 1)
  )
}

export function localPreviewMode({
  diff,
  language,
  navigationPending,
  onSelectRendered,
  target,
  userMode
}: LocalPreviewModeOptions) {
  const isMarkdown = (language || target.language) === 'markdown'
  const hasDiff = Boolean(diff && diff.trim())
  // Order the toggle reads left→right; default lands on the most useful view.
  const modes: PreviewViewMode[] = []

  if (isMarkdown || onSelectRendered) {
    modes.push('rendered')
  }

  modes.push('source')

  if (hasDiff) {
    modes.push('diff')
  }

  const autoMode: PreviewViewMode = hasDiff ? 'diff' : isMarkdown ? 'rendered' : 'source'

  // A fresh navigation outranks user mode. Once consumed, allow manual mode
  // changes; HTML's explicit Source handoff still outranks the diff default.
  const mode = navigationPending
    ? 'source'
    : userMode && modes.includes(userMode)
      ? userMode
      : onSelectRendered || target.renderMode === 'source' || target.location
        ? 'source'
        : autoMode

  return { mode, modes }
}
