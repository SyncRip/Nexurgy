// Authenticated blob access. <img src> / <a href> cannot send the bearer
// token, so blobs are fetched with fetch() and exposed as object URLs.

import { getBlob, getDrawingPackage } from './endpoints';

export interface BlobHandle {
  url: string;
  blob: Blob;
  /** Release the object URL. Call when the consumer unmounts. */
  revoke: () => void;
}

/** Fetch a blob with auth and return an object URL plus a revoke function. */
export async function blobUrl(sha256: string, opts: { signal?: AbortSignal } = {}): Promise<BlobHandle> {
  const blob = await getBlob(sha256, { signal: opts.signal });
  const url = URL.createObjectURL(blob);
  let revoked = false;
  return {
    url,
    blob,
    revoke: () => {
      if (revoked) return;
      revoked = true;
      URL.revokeObjectURL(url);
    },
  };
}

/** Save a Blob through a temporary link. */
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.rel = 'noopener';
  document.body.appendChild(a);
  a.click();
  a.remove();
  // Give the browser a moment to start the download before releasing the URL.
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

/** Fetch a blob with auth and trigger a browser download. */
export async function downloadBlob(sha256: string, filename: string, opts: { signal?: AbortSignal } = {}): Promise<void> {
  const blob = await getBlob(sha256, { filename, signal: opts.signal });
  saveBlob(blob, filename);
}

/** File name from a Content-Disposition header (filename*= wins over filename=); null when absent. Pure. */
export function filenameFromDisposition(header: string | null | undefined): string | null {
  if (!header) return null;
  const star = /filename\*\s*=\s*(?:UTF-8''|utf-8'')?("?)([^";]+)\1/i.exec(header);
  if (star) {
    try {
      return decodeURIComponent(star[2].trim());
    } catch {
      return star[2].trim();
    }
  }
  const plain = /filename\s*=\s*("?)([^";]+)\1/i.exec(header);
  return plain ? plain[2].trim() : null;
}

export interface PackageHandle extends BlobHandle {
  /** The server's file name (Content-Disposition), else `<fallback>`. */
  filename: string;
  /** X-Manifest-SHA256 of the package (null for drawings from before the package manifest). */
  manifestSha256: string | null;
}

/**
 * Authenticated object URL of a drawing's whole package (GET /drawings/{id}/package.zip), like blobUrl for
 * blobs: <a href> cannot send the bearer token. Call revoke() when done.
 */
export async function packageZipUrl(drawingId: string, opts: { signal?: AbortSignal; fallbackName?: string } = {}): Promise<PackageHandle> {
  const res = await getDrawingPackage(drawingId, { signal: opts.signal });
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  let revoked = false;
  return {
    url,
    blob,
    filename: filenameFromDisposition(res.headers.get('Content-Disposition')) || opts.fallbackName || `drawing_${drawingId.slice(0, 8)}_package.zip`,
    manifestSha256: res.headers.get('X-Manifest-SHA256'),
    revoke: () => {
      if (revoked) return;
      revoked = true;
      URL.revokeObjectURL(url);
    },
  };
}

/** Fetch the whole drawing package with auth and trigger a browser download. */
export async function downloadPackageZip(drawingId: string, opts: { signal?: AbortSignal; fallbackName?: string } = {}): Promise<void> {
  const h = await packageZipUrl(drawingId, opts);
  saveBlob(h.blob, h.filename);
  h.revoke();
}
