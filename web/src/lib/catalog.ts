// Small static catalogs that mirror server tables.

import type { FileKind } from '../api/types';

/** Materials known to planning and costing (server/app/domain/materials.py). */
export const KNOWN_MATERIALS = ['AL6061-T651', 'AL7075-T651', 'S45C', 'SUS304', 'POM'] as const;
export const DEFAULT_MATERIAL = 'AL6061-T651';

export function isKnownMaterial(m: string): boolean {
  return (KNOWN_MATERIALS as readonly string[]).includes(m);
}

/** Same extension map as server/app/api/files.py guess_kind(). */
const EXT_KIND: [string, FileKind][] = [
  ['.step', 'model_step'],
  ['.stp', 'model_step'],
  ['.stl', 'model_mesh'],
  ['.obj', 'model_mesh'],
  ['.glb', 'model_mesh'],
  ['.pdf', 'drawing_pdf'],
  ['.png', 'drawing_image'],
  ['.jpg', 'drawing_image'],
  ['.jpeg', 'drawing_image'],
  ['.webp', 'drawing_image'],
  ['.dxf', 'drawing_dxf'],
  ['.doc', 'document'],
  ['.docx', 'document'],
  ['.xls', 'document'],
  ['.xlsx', 'document'],
  ['.txt', 'document'],
  ['.md', 'document'],
];

export function guessKind(filename: string): FileKind {
  const n = filename.toLowerCase();
  for (const [ext, kind] of EXT_KIND) if (n.endsWith(ext)) return kind;
  return 'other';
}
