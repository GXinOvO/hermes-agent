import { useCallback, useEffect, useRef, useState } from 'react'

import { getApiRequestConnection } from '@/api/client'
import { desktopFsCacheKey, type DesktopFsSessionScope, writeDesktopFileText } from '@/lib/desktop-fs'
import { notifyWorkspaceChanged } from '@/store/workspace-events'

import { readTextPreview } from './preview-file-load'
import { previewFileScopeKey } from './preview-file-scope'

interface PreviewFileEditOptions {
  filePath: string
  scopeKey: string
  requestScope: DesktopFsSessionScope
  text?: string
  reloadKey: number
  onReload: () => void
  scopeChangedMessage: string
}

function editRouteKey(scope: DesktopFsSessionScope) {
  return scope.owner
    ? previewFileScopeKey(scope, null)
    : JSON.stringify([desktopFsCacheKey(), getApiRequestConnection()])
}

/** An editor lifetime is separate from its file authority. Cancel/reopen retires
 * the lifetime even when the same file is opened again; each Save freezes its
 * payload and only that lifetime may apply its asynchronous completion. */
export function usePreviewFileEdit({
  filePath,
  scopeKey,
  requestScope,
  text,
  reloadKey,
  onReload,
  scopeChangedMessage
}: PreviewFileEditOptions) {
  const [editing, setEditing] = useState(false)
  const [dirty, setDirty] = useState(false)
  const [editorKey, setEditorKey] = useState(0)
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<null | string>(null)
  const [conflict, setConflict] = useState(false)
  const draft = useRef('')
  const baseline = useRef('')
  const editRoute = useRef('')
  const editFile = useRef('')
  const currentFile = useRef('')
  const generation = useRef(0)
  const activeSave = useRef<object | null>(null)
  const capturedScopeKey = requestScope.owner ? scopeKey : undefined
  currentFile.current = JSON.stringify([scopeKey, filePath])

  const retire = useCallback(() => {
    generation.current += 1
    activeSave.current = null
  }, [])

  // eslint-disable-next-line no-restricted-syntax -- refs own editor resources, not mirrored reactive values
  useEffect(() => {
    retire()
    setEditing(false)
    setDirty(false)
    setSaving(false)
    setSaveError(null)
    setConflict(false)
    draft.current = ''
    baseline.current = ''

    return retire
  }, [filePath, reloadKey, capturedScopeKey, retire])

  const handleEditorChange = useCallback((value: string) => {
    draft.current = value
    setDirty(value !== baseline.current)
  }, [])

  const beginEdit = () => {
    retire()
    editRoute.current = editRouteKey(requestScope)
    editFile.current = currentFile.current
    baseline.current = text ?? ''
    draft.current = baseline.current
    setDirty(false)
    setEditorKey(key => key + 1)
    setSaving(false)
    setSaveError(null)
    setConflict(false)
    setEditing(true)
  }

  const cancelEdit = () => {
    retire()
    setEditing(false)
    setSaving(false)
    setSaveError(null)
    setConflict(false)
  }

  const discardAndReload = () => {
    cancelEdit()
    onReload()
  }

  const saveEdit = async (force = false) => {
    if (!editing || activeSave.current) {
      return
    }

    const operation = {
      generation: generation.current,
      content: draft.current,
      baseline: baseline.current,
      route: editRoute.current,
      file: editFile.current,
      scope: { ...requestScope, ...(requestScope.owner ? { owner: { ...requestScope.owner } } : {}) }
    }

    activeSave.current = operation
    setSaving(true)
    setSaveError(null)
    const isCurrent = () => generation.current === operation.generation && activeSave.current === operation

    const requireOwner = () => {
      if (editRouteKey(operation.scope) !== operation.route || currentFile.current !== operation.file) {
        throw new Error(scopeChangedMessage)
      }
    }

    try {
      requireOwner()

      if (!force) {
        let current

        try {
          current = await readTextPreview(filePath, undefined, operation.scope)
        } catch {
          // Preserve the existing best-effort conflict read; write errors surface.
        }

        if (!isCurrent()) {
          return
        }

        if (current && !current.binary && (current.text ?? '') !== operation.baseline) {
          setConflict(true)

          return
        }
      }

      if (!isCurrent()) {
        return
      }

      requireOwner()
      await writeDesktopFileText(filePath, operation.content, operation.scope)

      if (!isCurrent()) {
        return
      }

      requireOwner()
      baseline.current = operation.content
      const changedSinceSubmit = draft.current !== operation.content
      setDirty(changedSinceSubmit)
      setConflict(false)
      setEditing(changedSinceSubmit)
      notifyWorkspaceChanged()
      onReload()
    } catch (error) {
      if (isCurrent()) {
        setSaveError(error instanceof Error ? error.message : String(error))
      }
    } finally {
      if (isCurrent()) {
        activeSave.current = null
        setSaving(false)
      }
    }
  }

  return {
    editing,
    dirty,
    editorKey,
    saving,
    saveError,
    conflict,
    baseline: baseline.current,
    handleEditorChange,
    beginEdit,
    cancelEdit,
    discardAndReload,
    saveEdit
  }
}
