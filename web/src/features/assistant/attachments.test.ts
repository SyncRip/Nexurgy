import { describe, expect, it } from 'vitest';
import { fileLabel, givenSources, mentionsAttachment, shouldResolveAttachment, sourceState, sourceTone } from './attachments';

describe('attachment wording (design §2.5)', () => {
  it('recognises clear attachment wording only', () => {
    for (const t of ['按刚上传的 Markdown 生成装配', '按附件新建装配', '再按这份文档做', '用 BHD1.md 生成', '按 markdown 生成', 'build it from the attached file', 'see the uploaded spec'])
      expect(mentionsAttachment(t), t).toBe(true);
    for (const t of ['用 markdown 格式列出零件', 'output it as markdown', '说明一下这个装配', '新建一个静态装配：总装 A。', '这个零件为什么这么贵', 'commander'])
      expect(mentionsAttachment(t), t).toBe(false);
  });

  it('asks the server only while no file is referenced', () => {
    expect(shouldResolveAttachment('按附件生成', [])).toBe(true);
    expect(shouldResolveAttachment('按附件生成', [{ kind: 'part' }])).toBe(true);
    expect(shouldResolveAttachment('按附件生成', [{ kind: 'file_version' }])).toBe(false);
    expect(shouldResolveAttachment('   ', [])).toBe(false);
    // a pasted description that mentions an attachment in its body is material, not an instruction
    expect(shouldResolveAttachment('请新建装配：\n零件 A：长 10 mm，见附件 2。\n零件 B：长 20 mm。', [])).toBe(false);
    expect(shouldResolveAttachment('零件 A：长 10 mm。\n以上，按附件生成', [])).toBe(true);
  });
});

describe('spec sources (A-02)', () => {
  it('never reads an unknown state as read, and leaves answers to the spec page', () => {
    expect(['read', 'partial', 'failed', 'unread', 'pending'].map((s) => sourceState({ read_status: s }))).toEqual(['read', 'partial', 'failed', 'unread', 'unread']);
    expect(sourceTone('failed')).toBe('err');
    const src = (id: string, answer_of?: string) => ({ id, kind: 'text', file_version_id: null, name: '', label: id, version: null, read_status: 'read', ranges: [], error: null, answer_of });
    expect(givenSources([src('S-1'), src('S-2', 'Q-01')]).map((s) => s.id)).toEqual(['S-1']);
    expect(fileLabel({ name: 'gen02.md', version: 2 })).toBe('gen02.md v2');
    expect(fileLabel({ title: 'gen02.md', version_label: 'v1' })).toBe('gen02.md v1');
  });
});
