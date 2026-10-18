import { describe, expect, it } from 'vitest';
import { filenameFromDisposition } from './blobs';

describe('filenameFromDisposition', () => {
  it('reads plain and quoted file names', () => {
    expect(filenameFromDisposition('attachment; filename="PL-100_R2_D1_package.zip"')).toBe('PL-100_R2_D1_package.zip');
    expect(filenameFromDisposition('attachment; filename=plain.zip')).toBe('plain.zip');
  });
  it("prefers RFC 5987 filename*= and decodes it", () => {
    expect(filenameFromDisposition("attachment; filename=\"a.zip\"; filename*=UTF-8''%E5%9B%BE%E7%BA%B8.zip")).toBe('图纸.zip');
  });
  it('returns null when the header is missing (not exposed cross-origin)', () => {
    expect(filenameFromDisposition(null)).toBeNull();
    expect(filenameFromDisposition('inline')).toBeNull();
  });
});
