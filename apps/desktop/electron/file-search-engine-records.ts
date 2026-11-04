import path from 'node:path'

import type { FileSearchMatch } from '../../shared/src/file-search'

export const MAX_RG_RECORD_BYTES = 2 * 1024 * 1024
const MAX_SNIPPET_UNITS = 512
const MAX_RANGES = 128

/** Delimit bytes before decoding so a split UTF-8 code point stays intact. */
export class RgRecordReader {
  private parts: Buffer[] = []
  private size = 0

  constructor(
    private delimiter: number,
    private record: (value: Buffer) => boolean
  ) {}

  push(chunk: Buffer): boolean {
    let start = 0

    while (start < chunk.length) {
      const end = chunk.indexOf(this.delimiter, start)
      const piece = chunk.subarray(start, end < 0 ? chunk.length : end)
      this.size += piece.length

      if (this.size > MAX_RG_RECORD_BYTES) {
        throw new Error(`rg output record exceeds ${MAX_RG_RECORD_BYTES} bytes`)
      }

      this.parts.push(piece)

      if (end < 0) {
        return true
      }

      const value = Buffer.concat(this.parts, this.size)
      this.parts = []
      this.size = 0

      if (!this.record(value)) {
        return false
      }

      start = end + 1
    }

    return true
  }

  finish(): void {
    if (this.size) {
      throw new Error('rg output ended with an incomplete record')
    }
  }
}

interface RgText {
  text?: string
  bytes?: string
}
interface RgMatch {
  path: RgText
  lines: RgText
  line_number: number
  submatches: { start: number; end: number }[]
}

export function relativeRgPath(value: string): string {
  const relative = value
    .replace(/^\.[/\\]/, '')
    .split(path.sep)
    .join('/')

  if (
    !relative ||
    path.isAbsolute(relative) ||
    path.win32.isAbsolute(relative) ||
    relative.split('/').includes('..') ||
    relative.includes('\0')
  ) {
    throw new Error('rg returned a path outside the search root')
  }

  return relative
}

function textValue(value: RgText): Buffer {
  if (typeof value?.text === 'string') {
    return Buffer.from(value.text)
  }

  if (typeof value?.bytes === 'string') {
    return Buffer.from(value.bytes, 'base64')
  }

  throw new Error('Invalid rg text payload')
}

function boundary(source: string, index: number): number {
  const unit = source.charCodeAt(index)

  return unit >= 0xdc00 && unit <= 0xdfff ? index - 1 : index
}

export function contentHit(root: string, data: RgMatch): FileSearchMatch {
  const relative = relativeRgPath(textValue(data.path).toString('utf8'))
  const bytes = textValue(data.lines)
  const source = bytes.toString('utf8').replace(/\r?\n$/, '')

  if (
    !Number.isInteger(data.line_number) ||
    data.line_number < 1 ||
    !Array.isArray(data.submatches) ||
    !data.submatches.length
  ) {
    throw new Error('Invalid rg match metadata')
  }

  const first = data.submatches[0]
  const col = bytes.subarray(0, first.start).toString('utf8').length
  const start = boundary(source, Math.max(0, col - 160))
  const end = boundary(source, Math.min(source.length, start + MAX_SNIPPET_UNITS))
  const ranges: { from: number; to: number }[] = []
  let previousByte = first.start
  let previousUnit = col

  for (const match of data.submatches) {
    if (
      !Number.isInteger(match.start) ||
      !Number.isInteger(match.end) ||
      match.start < previousByte ||
      match.end < match.start ||
      match.end > bytes.length
    ) {
      throw new Error('Invalid rg match range')
    }

    const from = previousUnit + bytes.subarray(previousByte, match.start).toString('utf8').length

    if (from > end || ranges.length >= MAX_RANGES) {
      break
    }

    const to = from + bytes.subarray(match.start, match.end).toString('utf8').length
    ranges.push({ from: Math.max(0, from - start), to: Math.min(end, to) - start })
    previousByte = match.end
    previousUnit = to
  }

  return {
    path: path.join(root, relative),
    rel: relative,
    line: data.line_number,
    col,
    text: source.slice(start, end),
    ranges
  }
}
