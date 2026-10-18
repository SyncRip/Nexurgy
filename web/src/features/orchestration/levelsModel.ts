// Generation supplement E (layered delivery, BHD1 §8.7): pure helpers that read a delivery's per-level summary
// (server orchestration._deliver_levels) and a layered drawing step's sub-assembly packages. Unit-tested in
// levelsModel.test.ts.

import type { DeliveryLevel, DeliveryLevels, LevelFile, LevelObject, SubassemblyPackage } from '../../api/orchestration';

export const DELIVERY_LEVELS: readonly DeliveryLevel[] = ['assembly', 'subassembly', 'part'];

export interface LevelLine {
  level: DeliveryLevel;
  objects: number;
  /** STEP / DWG generated (missing and failed are what is left). */
  step: number;
  dwg: number;
  /** Every object of the level has both files. */
  complete: boolean;
}

/** One line per level (levels without objects are kept: an empty level is information too). */
export function levelLines(l: DeliveryLevels): LevelLine[] {
  return DELIVERY_LEVELS.map((level) => {
    const objs = l[level] ?? [];
    const step = objs.filter((o) => o.step.status === 'generated').length;
    const dwg = objs.filter((o) => o.dwg.status === 'generated').length;
    return { level, objects: objs.length, step, dwg, complete: step === objs.length && dwg === objs.length };
  });
}

/** Objects whose STEP or DWG is not there, with their level (what the BHD1 report has to list). */
export function levelGaps(l: DeliveryLevels): (LevelObject & { level: DeliveryLevel })[] {
  return DELIVERY_LEVELS.flatMap((level) =>
    (l[level] ?? []).filter((o) => o.step.status !== 'generated' || o.dwg.status !== 'generated').map((o) => ({ ...o, level })),
  );
}

/** "LV-01-SA A1" — number (else name) and revision of an object or a sub-assembly package. */
export function objectLabel(o: Pick<LevelObject, 'number' | 'name'> & { rev?: string | null; rev_no?: number | null }, revPrefix = 'A'): string {
  const rev = o.rev ?? (o.rev_no != null ? `${revPrefix}${o.rev_no}` : '');
  return [o.number || o.name || '?', rev].filter(Boolean).join(' ');
}

/** complete | partial | failed of one sub-assembly package (a missing package is failed). */
export function subPackageState(s: SubassemblyPackage): 'complete' | 'partial' | 'failed' | 'not_started' {
  // Generation reliability R: a package the step stopped before (budget / cancel / lease) was not drawn, not failed.
  if (s.completeness === 'not_started') return 'not_started';
  if (!s.package_id) return 'failed';
  return s.completeness === 'complete' ? 'complete' : 'partial';
}

/** Generation reliability M: the STEP protocol of an object against the delivery's required one. */
export interface ProtocolCell {
  /** AP203 / AP214 … read from the delivered file's header (null: no STEP). */
  protocol: string | null;
  met: boolean;
  /** The labelled other-protocol file kept when the required one could not be written. */
  alternative: string | null;
  /** The stored file was in this protocol and was re-exported. */
  reexportedFrom: string | null;
}

export function protocolCell(step: LevelFile, required = 'AP203'): ProtocolCell {
  const generated = step.status === 'generated';
  const protocol = generated ? step.protocol ?? null : step.alternative?.protocol ?? step.protocol ?? null;
  return {
    protocol,
    met: generated && protocol === required,
    alternative: step.alternative ? step.alternative.protocol ?? '?' : null,
    reexportedFrom: step.reexported_from ?? null,
  };
}

/** Representation level of an object (the build record's when there is one) and what its build did not create. */
export function representationCell(o: LevelObject): { level: string | null; notCreated: string[]; complete: boolean | null } {
  const rec = o.build_record;
  return {
    level: rec?.representation ?? o.representation ?? null,
    notCreated: (rec?.not_created ?? []).map((g) => g.requirement),
    complete: rec ? rec.complete : null,
  };
}
