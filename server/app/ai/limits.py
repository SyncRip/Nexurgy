"""Input limits and context budgets of every entry (S2, docs/agent/stability-fixes.zh-CN.md §S2): the one place
they are defined.

Request bodies (``max_length``), services, node parameters and the frontend (``maxLength``, read from
``GET /system/capabilities`` → ``limits``; web/src/api/limits.ts) all take them from here. Module constants that
other code already imports (``assistant_context.FILE_TEXT_REF_CHARS``, ``source_reader.CHUNK_CHARS``, …) are
aliases of these values. ``tests/test_input_limits.py`` fails when an entry's limit is hard-coded elsewhere again.

Input limits (characters):

* chat / modeling requests (:data:`CHAT_TEXT_CHARS`): one request typed in the AI workspace composer. A longer text
  is not refused there: it is saved as a project material (a text file version, at most
  :data:`MATERIAL_TEXT_CHARS`), referenced by the request and read according to the context budget
  (``services/assistant_context.py``: full → digest → chunked reading → an explicit failure naming what was not
  included). Entries without that material path (the task draft box and workflow text input)
  keep :data:`CHAT_TEXT_CHARS`.
* description-type fields (:data:`DESCRIPTION_CHARS`): approval comments, rejection reasons, notes, descriptions.
  They are meant to be short and keep 4,000.
* 2D drafting: the description a new drawing is drafted from (:data:`DRAWING_TEXT_CHARS`), TEXT / LEADER and MTEXT
  entity text (:data:`DRAFTING_TEXT_CHARS`, :data:`DRAFTING_MTEXT_CHARS`).

Context budgets (characters; token limits per model are :data:`MODEL_LIMIT_TABLE` below):

* AI workspace: the public modeling context block, a referenced text file / spec digest in a capped reference block,
  the digest of a material that does not fit in full and the notes one chunk read leaves.
* engineering spec reading (W0 §7): chunk size, overlap, the most chunks one version (or one material) reads and how
  many chunk reads run at the same time (S7); how many part definitions one assembly build models at the same time.
* collaboration planner input: spec digest, one document and the total.
"""

from __future__ import annotations

from typing import Any

# --- input limits ----------------------------------------------------------------------------------------

#: One chat / modeling request typed in the AI workspace (and the direct AI entries without a material path).
CHAT_TEXT_CHARS = 8000
#: The longest text accepted where a long text becomes a material (AI workspace) or a spec source (direct text).
MATERIAL_TEXT_CHARS = 2_000_000
#: Description-type fields: approval comments, rejection reasons, notes, descriptions, hints.
DESCRIPTION_CHARS = 4000
#: Longer notes: a deliverable's submission note, a milestone review note, recipe instructions.
LONG_NOTE_CHARS = 8000
#: The objective of a task / project / collaboration plan.
OBJECTIVE_CHARS = 10_000
#: An agent profile's role prompt.
ROLE_PROMPT_CHARS = 20_000
#: Project engineering-team preferences and reusable text skills; aggregate counts library text once.
TEAM_DISPLAY_NAME_CHARS = 80
TEAM_PROFILE_CHARS = 6000
TEAM_TONE_CHARS = 300
TEAM_PERSONALITY_CHARS = 1500
TEAM_SKILL_NAME_CHARS = 100
TEAM_SKILL_DESCRIPTION_CHARS = 500
TEAM_SKILL_INSTRUCTIONS_CHARS = 4000
TEAM_TOTAL_CHARS = 48_000
#: Hints of a drawing intake (the drawing is read from the file; the request only adds hints).
INTAKE_HINTS_CHARS = 4000
#: 2D drafting: the description a new drawing is drafted from.
DRAWING_TEXT_CHARS = 20_000
#: 2D drafting: the text of a TEXT / LEADER entity and of an MTEXT entity (also ``edit_text``).
#: Agent tool arguments (P1 typed tool registry): the free text of a modeling request.
TOOL_TEXT_CHARS = 20_000
DRAFTING_TEXT_CHARS = 2000
DRAFTING_MTEXT_CHARS = 8000

# --- context budgets -------------------------------------------------------------------------------------

#: ``Model3DParams.context``: the public target block of a modeling build (the run graph is project-visible).
MODEL_CONTEXT_CHARS = 6000
#: A referenced text file in a capped reference block (a target file is read completely).
FILE_TEXT_REF_CHARS = 12_000
#: A referenced engineering spec digest in a capped reference block (a target spec is complete).
SPEC_DIGEST_REF_CHARS = 24_000
#: The digest (outline + paragraph openings per chunk) of a material that does not fit in full.
MATERIAL_DIGEST_CHARS = 6000
#: The notes one chunk read of a material may leave (chunked reading: one call per chunk).
MATERIAL_NOTE_CHARS = 1500
#: Engineering spec reading (W0 §7): chunk size, overlap with the previous chunk (S4) and the most chunks one spec
#: version reads; a material read in chunks uses the same chunking and the same maximum.
SPEC_CHUNK_CHARS = 6_000
SPEC_CHUNK_OVERLAP_CHARS = 300
SPEC_MAX_CHUNKS = 80
#: Chunk reads of one spec version that run at the same time (S7: chunks are independent; model calls only).
SPEC_READ_CONCURRENCY = 3
#: Part definitions of one assembly build modeled at the same time (definitions are independent; the model calls and
#: kernel builds run in worker threads, every database write in the step's own thread). The assembly-from-spec node's
#: ``modeling_concurrency`` overrides it (1 … :data:`MODELING_CONCURRENCY_MAX`; 1 = one after another).
MODELING_CONCURRENCY = 3
MODELING_CONCURRENCY_MAX = 8
#: Collaboration planner input: the spec digest, one text document and everything together.
PLANNER_SPEC_CHARS = 40_000
PLANNER_DOC_CHARS = 24_000
PLANNER_TOTAL_CHARS = 80_000

# --- model token limits ----------------------------------------------------------------------------------

#: (model id prefix, input / window tokens, max output tokens); first match wins, most specific first.
#: The input limit is treated as the window shared with the output (conservative for models whose
#: output budget is separate, e.g. Gemini). The context builder budgets input with it
#: (``services/assistant_context.model_limits``); the OpenAI-compatible adapter asks for the output budget
#: (:func:`output_token_limit`).
MODEL_LIMIT_TABLE: tuple[tuple[str, int, int], ...] = (
    # Google model spec (verified 2026-10-17): unified window; synchronous output is 65,536.
    # The 1M output option requires long decode, which the current adapters do not enable.
    # https://ai.google.dev/gemini-api/docs/models/gemini-4-argon
    ("gemini-4-argon", 1_048_576, 65_536),
    ("gemini-3", 1_048_576, 65_536),
    ("gemini-2.5", 1_048_576, 65_536),
    ("gemini-2.0", 1_048_576, 8_192),
    ("gemini-1.5", 1_048_576, 8_192),
    ("gpt-4.1", 1_047_576, 32_768),
    ("gpt-4o", 128_000, 16_384),
    # gpt-6.x retains provisional gpt-5 budgets until verified limits are available.
    ("gpt-6", 400_000, 128_000),
    ("gpt-5", 400_000, 128_000),
    ("o3", 200_000, 100_000),
    ("o4", 200_000, 100_000),
)


def table_limits(model_id: str, table: tuple[tuple[str, int, int], ...] | None = None) -> tuple[int, int] | None:
    """(input / window tokens, max output tokens) of the first prefix of ``table`` (default :data:`MODEL_LIMIT_TABLE`)
    that ``model_id`` starts with; None for an unlisted id. A provider prefix ("models/…", "google/…") is ignored."""
    mid = (model_id or "").strip().lower().rsplit("/", 1)[-1]
    for prefix, inp, out in MODEL_LIMIT_TABLE if table is None else table:
        if mid.startswith(prefix):
            return inp, out
    return None


def output_token_limit(model_id: str, capabilities: Any = None) -> int | None:
    """The output budget an adapter asks for, reasoning tokens included: ``capabilities.max_output_tokens`` if a
    configuration ever declares one, else the table's. None for an unlisted model id: nothing is sent and the
    server's default applies (the table's highest value could exceed what a small local model accepts)."""
    declared = getattr(capabilities, "max_output_tokens", None)
    if isinstance(declared, int) and declared > 0:
        return declared
    known = table_limits(model_id)
    return known[1] if known else None


def public() -> dict[str, Any]:
    """The limits as ``GET /system/capabilities`` returns them (``limits``): what the frontend's ``maxLength`` and
    material hints read. Keys are stable API names."""
    return {
        "inputs": {
            "chat": CHAT_TEXT_CHARS,
            "material": MATERIAL_TEXT_CHARS,
            "description": DESCRIPTION_CHARS,
            "long_note": LONG_NOTE_CHARS,
            "objective": OBJECTIVE_CHARS,
            "role_prompt": ROLE_PROMPT_CHARS,
            "team_display_name": TEAM_DISPLAY_NAME_CHARS,
            "team_profile": TEAM_PROFILE_CHARS,
            "team_tone": TEAM_TONE_CHARS,
            "team_personality": TEAM_PERSONALITY_CHARS,
            "team_skill_name": TEAM_SKILL_NAME_CHARS,
            "team_skill_description": TEAM_SKILL_DESCRIPTION_CHARS,
            "team_skill_instructions": TEAM_SKILL_INSTRUCTIONS_CHARS,
            "team_total": TEAM_TOTAL_CHARS,
            "intake_hints": INTAKE_HINTS_CHARS,
            "drawing_text": DRAWING_TEXT_CHARS,
            "drafting_text": DRAFTING_TEXT_CHARS,
            "drafting_mtext": DRAFTING_MTEXT_CHARS,
        },
        "context": {
            "model_context": MODEL_CONTEXT_CHARS,
            "file_text_ref": FILE_TEXT_REF_CHARS,
            "spec_digest_ref": SPEC_DIGEST_REF_CHARS,
            "material_digest": MATERIAL_DIGEST_CHARS,
            "material_note": MATERIAL_NOTE_CHARS,
            "chunk": SPEC_CHUNK_CHARS,
            "chunk_overlap": SPEC_CHUNK_OVERLAP_CHARS,
            "max_chunks": SPEC_MAX_CHUNKS,
            "read_concurrency": SPEC_READ_CONCURRENCY,
            "modeling_concurrency": MODELING_CONCURRENCY,
            "modeling_concurrency_max": MODELING_CONCURRENCY_MAX,
            "planner_spec": PLANNER_SPEC_CHARS,
            "planner_doc": PLANNER_DOC_CHARS,
            "planner_total": PLANNER_TOTAL_CHARS,
        },
    }
