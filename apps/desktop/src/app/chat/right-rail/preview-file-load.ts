import { fileUrlToNativePath } from '@hermes/shared'
import { useStore } from '@nanostores/react'
import { useEffect, useMemo, useState } from 'react'

import {
  desktopFileDiff,
  DesktopFileMissingError,
  type DesktopFsSessionScope,
  desktopGitRoot,
  isReadFileErrorResult,
  readDesktopFileDataUrl,
  readDesktopFileText
} from '@/lib/desktop-fs'
import { markPreviewTabMissing, type PreviewTarget } from '@/store/preview'
import { $connection } from '@/store/session'

import { previewFileScopeKey } from './preview-file-scope'

const TEXT_PREVIEW_MAX_BYTES = 512 * 1024

interface LocalPreviewState {
  /** The read whose text may consume a source-navigation request. */
  contentScope?: string
  binary?: boolean
  byteSize?: number
  dataUrl?: string
  /** Working-tree-vs-HEAD unified diff, when the file has uncommitted changes. */
  diff?: string
  error?: string
  language?: string
  /** The read confirmed the file is gone (not a transient failure) — renders
   *  the explicit tombstone and prunes the tab from future restores. */
  missing?: boolean
  text?: string
  loading: boolean
  truncated?: boolean
}

interface LocalPreviewLoadOptions {
  forcePreview: boolean
  reloadKey: number
  selfReload: number
  sessionId?: string
  target: PreviewTarget
}

function filePathForTarget(target: PreviewTarget) {
  if (target.path) {
    return target.path
  }

  return fileUrlToNativePath(target.url) ?? target.url
}

function looksBinaryBytes(bytes: Uint8Array) {
  if (!bytes.length) {
    return false
  }

  let suspicious = 0

  for (const byte of bytes.slice(0, 4096)) {
    if (byte === 0) {
      return true
    }

    if (byte < 32 && byte !== 9 && byte !== 10 && byte !== 13) {
      suspicious += 1
    }
  }

  return suspicious / Math.min(bytes.length, 4096) > 0.12
}

export async function readTextPreview(filePath: string, sessionId?: string, scope?: DesktopFsSessionScope) {
  const requestScope = scope ?? { sessionId }

  try {
    return await readDesktopFileText(filePath, requestScope)
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error)

    if (!message.includes("No handler registered for 'hermes:readFileText'")) {
      throw error
    }
  }

  // Back-compat for a running Electron process whose preload hasn't been
  // restarted since readFileText was added. readFileDataUrl already existed.
  const dataUrl = await readDesktopFileDataUrl(filePath, requestScope)

  if (isReadFileErrorResult(dataUrl)) {
    throw new Error(dataUrl.message || `File read failed: ${dataUrl.error}`)
  }

  const [, metadata = '', data = ''] = dataUrl.match(/^data:([^,]*),(.*)$/) || []
  const base64 = metadata.includes(';base64')
  const mimeType = metadata.replace(/;base64$/, '') || undefined
  const raw = base64 ? atob(data) : decodeURIComponent(data)
  const bytes = Uint8Array.from(raw, ch => ch.charCodeAt(0))

  return {
    binary: looksBinaryBytes(bytes),
    byteSize: bytes.byteLength,
    mimeType,
    path: filePath,
    text: new TextDecoder().decode(bytes)
  }
}

export function useLocalPreviewLoad({
  forcePreview,
  reloadKey,
  selfReload,
  sessionId,
  target
}: LocalPreviewLoadOptions) {
  const [state, setState] = useState<LocalPreviewState>({ loading: true })
  const connection = useStore($connection)
  const requestScope = useMemo(() => target.fileScope ?? { sessionId }, [sessionId, target.fileScope])
  const scopeKey = previewFileScopeKey(requestScope, connection)
  const filePath = filePathForTarget(target)
  const contentScope = JSON.stringify([scopeKey, filePath, reloadKey, selfReload])
  const navigationReady = state.contentScope === contentScope && !state.loading
  const isImage = target.previewKind === 'image'
  const isPdf = target.previewKind === 'pdf'
  // HTML source uses the text path; the pane owns its rendered webview.
  // Binary targets reach this path after the user admits the preview.
  const isText = target.previewKind === 'text' || target.previewKind === 'binary' || target.previewKind === 'html'
  const blockedByTarget = !isImage && !isPdf && !forcePreview && (target.binary || target.large)

  useEffect(() => {
    let active = true

    async function loadText() {
      const result = await readTextPreview(filePath, undefined, requestScope)

      if (!active) {
        return
      }

      const shouldBlock = !forcePreview && (result.binary || (result.byteSize ?? 0) > TEXT_PREVIEW_MAX_BYTES)

      setState({
        binary: result.binary,
        byteSize: result.byteSize,
        contentScope,
        language: result.language || target.language || 'text',
        loading: false,
        text: shouldBlock ? undefined : result.text,
        truncated: result.truncated
      })

      // Best-effort: clean files, non-repositories and remote reads may have no
      // diff. Only enrich the active read; never replace newer content.
      if (!shouldBlock && !requestScope.owner) {
        try {
          const root = await desktopGitRoot(filePath)
          const diff = root ? await desktopFileDiff(root, filePath) : ''

          if (active && diff.trim()) {
            setState(prev => (prev.text === result.text ? { ...prev, diff } : prev))
          }
        } catch {
          // No diff available; the preview just shows source.
        }
      }
    }

    async function load() {
      if (blockedByTarget) {
        setState({ loading: false })

        return
      }

      if (!isImage && !isPdf && !isText) {
        setState({ loading: false })

        return
      }

      setState({ loading: true })

      try {
        if (isImage || isPdf) {
          // Prefer caller-supplied bytes: a pasted/dropped image's path may be
          // transient or unreadable.
          const dataUrl = target.dataUrl || (await readDesktopFileDataUrl(filePath, requestScope))

          if (active) {
            setState({ dataUrl, loading: false })
          }

          return
        }

        await loadText()
      } catch (error) {
        if (active) {
          // Confirmed absence tombstones the tab; transient failures stay retryable.
          const missing = error instanceof DesktopFileMissingError

          if (missing) {
            markPreviewTabMissing(target.url)
          }

          setState({
            error: error instanceof Error ? error.message : String(error),
            loading: false,
            missing
          })
        }
      }
    }

    void load()

    return () => {
      active = false
    }
  }, [
    blockedByTarget,
    contentScope,
    filePath,
    forcePreview,
    scopeKey,
    requestScope,
    isImage,
    isPdf,
    isText,
    reloadKey,
    selfReload,
    target.dataUrl,
    target.language,
    target.url
  ])

  const shouldBlock =
    !isImage &&
    !isPdf &&
    !forcePreview &&
    (target.binary || target.large || state.binary || (state.byteSize ?? 0) > TEXT_PREVIEW_MAX_BYTES)

  return { blockedByTarget, filePath, isImage, isPdf, isText, navigationReady, requestScope, scopeKey, shouldBlock, state }
}
