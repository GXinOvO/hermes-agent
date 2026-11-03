// The right sidebar's copy (file tree, folder picker, terminals); `Translations.rightSidebar`.
export interface RightSidebarTranslations {
  aria: string
  panelsAria: string
  files: string
  terminal: string
  noFolderSelected: string
  changeCwdTitle: string
  remotePickerTitle: string
  remotePickerDescription: string
  remotePickerSelect: string
  remotePickerNewFolder: string
  remotePickerFolderName: string
  remotePickerCreateFolder: string
  remotePickerInvalidFolderName: string
  remotePickerCreateFolderFailed: (error: string) => string
  folderTip: (cwd: string) => string
  openFolder: string
  refreshTree: string
  collapseAll: string
  showIgnored: string
  hideIgnored: string
  filesSearchToggle: string
  filesSearchClose: string
  filesSearchNameMode: string
  filesSearchContentMode: string
  filesSearchPlaceholderName: string
  filesSearchPlaceholderContent: string
  filesSearchScope: string
  filesSearchScopePlaceholder: string
  filesSearchCase: string
  filesSearchRegex: string
  filesSearchHint: string
  filesSearchNoResults: string
  filesSearchStatus: (hits: number, files: number) => string
  filesSearchScanning: (files: number, hits: number) => string
  filesSearchTruncated: string
  filesSearchInvalidPattern: (error: string) => string
  previewUnavailable: string
  couldNotPreview: (path: string) => string
  noProjectTitle: string
  noProjectBody: string
  noProjectOpen: string
  noDiffs: string
  unreadableTitle: string
  unreadableBody: (error: string) => string
  emptyTitle: string
  emptyBody: string
  treeErrorTitle: string
  treeErrorBody: string
  tryAgain: string
  loadingTree: string
  loadingFiles: string
  terminalHide: string
  terminalsAria: string
  terminalNew: string
  terminalReadOnly: string
  terminalReadOnlyHelp: string
  terminalOpenInteractive: string
  terminalCloseOthers: string
  terminalCloseAll: string
  terminalNewOn: string
  terminalGatewayRemoved: string
  terminalGatewayRemovedBody: string
  terminalGatewayNoShellBody: string
  terminalDisconnectedBody: string
  terminalDropUploadFailed: (name: string) => string
  terminalDropTooLargeTitle: string
  terminalDropTooLarge: (names: string, limit: string) => string
  terminalDropControlTitle: string
  terminalDropControl: (paths: string) => string
  terminalDropUnquotable: (shell: string, paths: string) => string
  addToChat: string
}
