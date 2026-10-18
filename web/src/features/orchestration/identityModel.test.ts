import { describe, expect, it } from 'vitest';
import { coverageSummary } from '../assistant/attachments';
import { identityView } from './identityModel';

const target = { basis: 'spec', definitions: 4, instances: 6, subassemblies: 1, subassembly_instances: 1, recognized_definitions: 5 };

describe('identity and source coverage (generation reliability K, design §2.4–§2.5)', () => {
  it('shows the five classes and the original target next to the current one', () => {
    expect(identityView({ target })).toBeNull(); // a block from before: nothing invented
    const v = identityView({ target, identity: { parts: 4, subassemblies: 1, aliases: 1, excluded: 2, unresolved: 1 },
      baseline: { spec_version_id: null, label: 'v1', definitions: 3, instances: 6, subassemblies: 1 } });
    expect(v?.counts).toEqual({ parts: 4, subs: 1, aliases: 1, excluded: 2, unresolved: 1, unbound: 0 });
    expect(v?.baseline).toEqual({ label: 'v1', defs: 3, insts: 6 });
    expect(v?.changed).toBe(true);
    expect(identityView({ target, baseline: { spec_version_id: null, label: 'v1', definitions: 4, instances: 6, subassemblies: 1 } })?.changed).toBe(false);
  });

  it('keeps chunk progress and entity coverage apart', () => {
    const s = coverageSummary({ frames: 38, chunks: 46, chunks_sent: 46,
      entities: { total: 9000, in_frames: 7200, quarantined: 20, outside: { read: 900, excluded: { geometry_only: 860 }, unexplained: 20 } } });
    expect([s.sent, s.chunks, s.read, s.excluded, s.unexplained]).toEqual([46, 46, 900, 860, 20]);
    expect(s.reasons).toEqual([{ reason: 'geometry_only', n: 860 }]);
  });
});
