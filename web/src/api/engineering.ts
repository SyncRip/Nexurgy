// Shared general-mechanical-engineering types (docs/development/mechanical-engineering/w0-contract.zh-CN.md).
// Execution, engineering check and acceptance are three separate dimensions; never fold them into one "ok".

/** Frozen per request before planning from the saved project policy; new projects default to turbo. */
export type ExecutionPolicy = 'standard' | 'turbo';
export type CheckStatus = 'not_requested' | 'skipped_turbo' | 'pending' | 'passed' | 'failed' | 'unknown';
export type AcceptanceStatus = 'not_requested' | 'pending' | 'accepted' | 'rejected';
export type RepresentationLevel = 'exact' | 'nominal' | 'simplified';
export type ProcurementKind = 'make' | 'buy' | 'standard' | 'unknown';
export type FactStatus = 'asserted' | 'derived' | 'assumed' | 'conflicting' | 'unknown';
export type TargetKind = 'part_revision' | 'assembly_revision' | 'drawing_document_version';

/** A value adopted for a missing / conflicting fact (server: app/policy.py Assumption). */
export interface Assumption {
  id: string;
  subject: string;
  field: string;
  adopted: unknown;
  unit: string | null;
  basis: string;
  alternatives: unknown[];
  affected: string[];
  sources: string[];
  author: 'ai_turbo' | 'ai_design' | 'rule' | 'person';
  at: string;
  /** Generation supplement D-02: why a decision was needed (absent on older records). */
  trigger?: 'missing' | 'ambiguous' | 'conflict' | null;
  /** A person's decision recorded in a later version's content (W6); revisions / drawings keep separate records. */
  confirmation?: { author: 'person'; by: string | null; at: string | null; value: unknown; decision: 'confirmed' | 'corrected' } | null;
}

/** The short form of an AI / rule assumption still waiting for a person (verification.unconfirmed_assumptions). */
export interface AssumptionRef {
  id: string;
  subject: string;
  field?: string | null;
  adopted?: unknown;
  unit?: string | null;
  author?: Assumption['author'] | null;
  /** The artifact that carries it (a drawing lists its source revision's assumptions with that owner). */
  owner?: { type: string; id: string; rev_no?: number } | string | null;
}

/** Carried by every derived artifact (server: app/policy.py verification()). Verified only when every skipped check
 * re-ran and passed (``pending_checks`` empty) AND a person confirmed every AI / rule assumption
 * (``unconfirmed_assumptions`` empty); a derived artifact also needs its upstream verified. */
export interface Verification {
  execution_policy: ExecutionPolicy;
  check_status: CheckStatus;
  acceptance_status: AcceptanceStatus;
  unverified: boolean;
  assumptions: Assumption[];
  pending_checks?: string[];
  unconfirmed_assumptions?: AssumptionRef[];
}

/** 4x4 row-major rigid transform, child-local → parent (server: app/cad/assembly_model.py). */
export type Matrix4 = number[][];
