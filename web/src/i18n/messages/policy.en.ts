// English messages for S5 (execution-policy consistency), merged into en.ts. Only S5 adds keys here; keys must match
// policy.zh.ts. `pol.*` = where a request's policy comes from, the "confirm these assumptions" entry and the project's
// "default Turbo" switch; the other prefixes are the new server codes.
export const policyEn = {
  // where the policy comes from (explicit > inherited > project default > standard)
  'pol.source.label': 'Policy source',
  'pol.source.explicit': 'Chosen for this request',
  'pol.source.inherited': 'Inherited from {label}',
  'pol.source.project_default': 'Project default: Turbo',
  'pol.source.default': 'Default',
  'pol.source.short.explicit': 'chosen',
  'pol.source.short.inherited': 'inherited',
  'pol.source.short.project_default': 'project default',
  'pol.source.short.default': 'default',
  'pol.source.of': '{label} ran in {policy}.',
  'pol.follow': 'Follow the source',
  'pol.loading': 'Checking the policy…',

  // what an explicit switch means
  'pol.cons.confirm_required': 'Standard will require confirming the {n} upstream assumption(s) first.',
  'pol.cons.confirm_upstream': 'Standard: {n} upstream assumption(s) are not confirmed; the result stays not verified until a person confirms them.',
  'pol.cons.corrected': 'A person corrected {n} upstream value(s): make a new version with them before a standard run.',
  'pol.cons.turbo_over_standard': 'The source ran in standard; Turbo skips checks and marks the result not verified.',

  // confirm these assumptions (one list, confirmed in one go)
  'pol.confirm.open': 'Confirm these assumptions',
  'pol.confirm.title': 'Confirm upstream assumptions',
  'pol.confirm.hint':
    'These values were adopted by AI or a rule; nobody stated them. Confirm only what you have checked: you are recorded as the confirmer. A wrong value is an engineering change (a new version), not changed here.',
  'pol.confirm.counts': '{pending} to confirm · {confirmed} confirmed · {corrected} corrected',
  'pol.confirm.all': 'Confirm all ({n})',
  'pol.confirm.one': 'Confirm',
  'pol.confirm.running': 'Confirming…',
  'pol.confirm.done': 'Confirmed {n} assumption(s)',
  'pol.confirm.none': 'Nothing upstream waits for a person.',
  'pol.confirm.blocked': 'A corrected value blocks a standard takeover: make a new version with it first.',
  'pol.confirm.retryHint': 'After confirming, retry the step: it runs in standard with these values as confirmed facts.',
  'pol.confirm.state.unconfirmed': 'To confirm',
  'pol.confirm.state.confirmed': 'Confirmed',
  'pol.confirm.state.corrected': 'Corrected',
  'pol.confirm.by': 'by {name}',
  'pol.confirm.correctedTo': 'corrected to {value}: needs a new version',
  'pol.confirm.owner.engineering_spec_version': 'Spec {label}',
  'pol.confirm.owner.assembly_revision': 'Assembly {label}',
  'pol.confirm.owner.model_revision': 'Part {label}',
  'pol.confirm.owner.other': '{label}',

  // assembly revision: Confirm assumptions
  'pol.asm.title': 'Assumptions to confirm',
  'pol.asm.hint': 'Values this revision rests on that AI or a rule adopted. It is verified only when every check passed and a person confirmed every value (its own, its spec’s, its parts’).',
  'pol.asm.pending': 'Checks still to pass: {list}',
  'pol.asm.upstream': '{n} part / sub-assembly revision(s) it is made of are not verified yet.',
  'pol.asm.ofSpec': 'decided on the spec',
  'pol.asm.verified': 'Verified: every check passed and every value is confirmed.',

  // project settings: "default Turbo"
  'pol.project.title': 'Default Turbo',
  'pol.project.hint':
    'New requests use the current project default, including new runs and re-extractions from older results. Turbo adopts missing values as assumptions, skips engineering checks and acceptance, and marks results not verified. Existing runs, retries and child operations retain their saved settings.',
  'pol.project.on': 'On: new requests use Turbo',
  'pol.project.off': 'Off: new requests use standard',

  // server codes
  'error.upstream_assumptions_unconfirmed': 'This Turbo spec holds AI-adopted values: a person must confirm them before a standard run uses it.',
  'error.upstream_assumptions_corrected': 'A person corrected a value of this Turbo spec: answer it on the spec page (a new version) and run with that version.',
  'error.turbo_clarification_refused': 'A step asked for clarification inside a Turbo run; Turbo never waits for answers.',
  'error.invalid_source_type': 'Unknown source type.',
  'wf.nerr.upstream_assumptions_unconfirmed': 'This Turbo spec holds AI-adopted values: confirm them, then retry this step to continue in standard.',
  'wf.nerr.upstream_assumptions_corrected': 'A person corrected a value of this Turbo spec: make a new spec version with it and run from that version.',
  'wf.nerr.turbo_clarification_refused': 'A step asked for clarification inside a Turbo run (a protocol error); upstream values are known facts.',
};

export type PolicyKey = keyof typeof policyEn;
