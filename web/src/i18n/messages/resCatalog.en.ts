// English messages of the resources & knowledge catalogue (business types, creation methods, preview and status
// states shared by the materials list and detail). Keys must match resCatalog.zh.ts.
export const resCatalogEn = {
  'rk.type.model': '3D model',
  'rk.type.drawing': 'Drawing',
  'rk.type.spec': 'Engineering spec',
  'rk.type.process_plan': 'Process plan',
  'rk.type.quote': 'Quote',
  'rk.type.document': 'Document',
  'rk.type.process_module': 'Process module',
  'rk.type.intake': 'Drawing intake',
  'rk.type.delivery': 'Delivery package',

  'rk.creation.upload': 'Uploaded file',
  'rk.creation.manual': 'Created by hand',
  'rk.creation.task': 'Task output',
  'rk.creation.unknown': 'Not identified',

  'rk.preview.preview': 'Preview',
  'rk.preview.page': 'Open in its page',
  'rk.preview.convert': 'Needs conversion',
  'rk.preview.download': 'Download only',
  'rk.preview.failed': 'Conversion failed',
  'rk.preview.none': 'Nothing to show yet',

  'rk.status.deprecated': 'Current version deprecated',
  'rk.status.upstream_newer': 'Upstream has a newer version',
  'rk.status.published': 'Published',
  'rk.status.failed': 'Generation failed',

  // Raw kinds that are not asset types (asset types use as.type.*).
  'rk.subtype.file': 'File (not catalogued)',
  'rk.subtype.engineering_spec': 'Engineering spec',
} as const;

export type ResCatalogKey = keyof typeof resCatalogEn;
