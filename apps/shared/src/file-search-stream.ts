import type { FileSearchFrame } from './file-search'

/** Shared by the native HTTP reader and same-origin Webapp reader. A truncated
 * connection is an error, never a complete empty answer. */
export function fileSearchDecoder(onFrame: (frame: FileSearchFrame) => void) {
  const decoder = new TextDecoder()
  let buffered = ''
  let terminal = false
  let bytes = 0

  function line(value: string) {
    if (!value.trim()) {
      return
    }

    if (terminal) {
      throw new Error('Search stream continued after its terminal frame')
    }
    const frame = JSON.parse(value) as FileSearchFrame

    if (!frame || !['batch', 'done', 'error'].includes(frame.type)) {
      throw new Error('Invalid search frame')
    }

    if (frame.type === 'batch' && !Array.isArray(frame.hits)) {
      throw new Error('Invalid search batch')
    }
    terminal = frame.type === 'done' || frame.type === 'error'
    onFrame(frame)
  }

  return {
    push(chunk: Uint8Array) {
      bytes += chunk.byteLength

      if (bytes > 12 * 1024 * 1024) {
        throw new Error('Search response exceeded its size limit')
      }
      buffered += decoder.decode(chunk, { stream: true })
      const lines = buffered.split('\n')
      buffered = lines.pop() || ''

      if (buffered.length > 2 * 1024 * 1024) {
        throw new Error('Search frame exceeded its size limit')
      }

      for (const value of lines) {
        line(value)
      }
    },
    finish() {
      buffered += decoder.decode()

      if (buffered) {
        line(buffered)
      }

      if (!terminal) {
        throw new Error('Search stream ended without a terminal frame')
      }
    }
  }
}
