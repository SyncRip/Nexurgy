// Display labels of the catalogue's closed sets (resCatalog messages), shared by the list and the detail.

import { hasKey } from '../../i18n/core';
import type { TFunction } from '../../i18n/I18nProvider';
import type { BusinessType, CatalogStatus, CreationMethod, PreviewState } from './types';

export const typeLabel = (t: TFunction, type: BusinessType) => t(`rk.type.${type}`);
export const creationLabel = (t: TFunction, c: CreationMethod) => t(`rk.creation.${c}`);
export const previewLabel = (t: TFunction, p: PreviewState) => t(`rk.preview.${p}`);
export const statusLabel = (t: TFunction, s: CatalogStatus) => t(`rk.status.${s}`);

/** A raw kind (CatalogItem.subtype): the catalogue's own rows (file, engineering_spec), else the asset type name. */
export function subtypeLabel(t: TFunction, subtype: string): string {
  const own = `rk.subtype.${subtype}`;
  if (hasKey(own)) return t(own);
  const asset = `as.type.${subtype}`;
  return hasKey(asset) ? t(asset) : subtype.replace(/_/g, ' ');
}
