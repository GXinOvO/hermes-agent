/** Search locations use one-based lines and UTF-16 columns, like CodeMirror. */
export interface PreviewLocation {
  column?: number
  line: number
}
