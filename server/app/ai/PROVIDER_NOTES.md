# Model provider notes (app/ai)

Installed SDKs: `google-genai` 2.28.0, `openai` 3.28.0 (uses `httpx2`), `pydantic-ai-slim[openai,google]` 2.55.0 (tool_agent
only). Signatures were checked in the installed package source; protocol choices were checked against the official docs below.
openai went 3.24.0 → 3.28.0 with pydantic-ai (2026-10-11); `tests/ai` (both adapters, streaming) passed unchanged after the bump.

## Docs consulted (read 2026-10-02)

| Topic | URL |
|---|---|
| Gemini structured output | https://ai.google.dev/gemini-api/docs/structured-output |
| Gemini generateContent / GenerationConfig reference | https://ai.google.dev/api/generate-content |
| Gemini API errors | https://ai.google.dev/gemini-api/docs/api-errors |
| Gemini troubleshooting (retryable codes) | https://ai.google.dev/gemini-api/docs/troubleshooting |
| Gemini document (PDF) input | https://ai.google.dev/gemini-api/docs/document-processing |
| Gemini models | https://ai.google.dev/gemini-api/docs/models |
| OpenAI Structured Outputs | https://developers.openai.com/api/docs/guides/structured-outputs |
| OpenAI images / vision | https://developers.openai.com/api/docs/guides/images-vision |
| OpenAI PDF file inputs | https://developers.openai.com/api/docs/guides/pdf-files |
| OpenAI error codes | https://developers.openai.com/api/docs/guides/error-codes |

## Gemini (`providers/gemini.py`)

- Auth: `genai.Client(api_key=..., http_options=HttpOptions(retry_options=HttpRetryOptions(attempts=1), base_url=...))` (no SDK
  retry: the bounded retry of transient failure classes runs in `Tracer.model_call`, `ai/retry.py`, one ai_calls row per request).
- Call: `client.models.generate_content(model, contents, config=GenerateContentConfig(system_instruction,
  response_mime_type="application/json", response_json_schema=<gemini dialect>, temperature?, http_options=HttpOptions(timeout=ms)))`.
  The current structured-output guide shows its examples on the newer Interactions API
  (`client.interactions.create(response_format={"type": "text", "mime_type": "application/json", "schema": ...})`).
  We use `generateContent` because the API reference still lists it as active, and it is the stable surface of this SDK.
  The schema dialect is the same in both. `GenerationConfig.response_format` is documented in the SDK as not supported
  on the Gemini API (Vertex only).
- Attachments: `types.Part.from_bytes(data, mime_type)` (PNG/JPEG/WEBP/PDF) on the last user turn; ≤ 20 MB inline total (our limit).
- Reply: `resp.text` (thought parts excluded), `usage_metadata.prompt_token_count` / `candidates_token_count + thoughts_token_count`,
  `response_id`, `model_version`. Blocked prompts (`prompt_feedback.block_reason`) and finish reasons such as SAFETY,
  RECITATION or PROHIBITED_CONTENT raise `bad_response`. A MAX_TOKENS finish with broken JSON also raises `bad_response`.
- Errors (`map_gemini_error`): 401/UNAUTHENTICATED, 400 with API_KEY_INVALID, or a leaked/invalid key on 403 → `auth_failed`;
  403 → `permission_denied`; FAILED_PRECONDITION (billing/region) → `permission_denied`; 404 → `model_not_found`;
  402 or a per-day/billing 429 → `quota_exceeded` (not retryable); other 429 → `rate_limited` (RetryInfo `retryDelay` → `retry_after_s`);
  408/504 → `timeout`; other 4xx → `invalid_request`; 5xx → `network` (retryable); httpx timeouts / transport errors → `timeout` / `network`.
- `test_connection`: `models.get` (no token cost; checks the key, the model id and `supported_actions`), then a tiny structured probe.
  If the config declares vision, the probe attaches a 16×16 red PNG and expects `"color": "red"`.

## OpenAI (`providers/openai_provider.py::OpenAIProvider`) — Responses API

- `OpenAI(api_key, base_url=config.base_url or None, max_retries=0)` (retries: `ai/retry.py`); per call `timeout=` (per-read HTTP timeout, see
  "Streaming and time limits").
- `client.responses.create(model, instructions=system, input=[{role, content}], store=False, stream=True,
  text={"format": {"type": "json_schema", "name", "schema": <openai_strict dialect>, "strict": True}}, temperature?)`.
- CAD modeling (create / modify / drawing reconstruction, including repair calls) enters the public
  `provider.prefer_prompt_schema()` scope. Responses uses `text.format.type="json_object"` there, with the complete
  portable reply schema appended to `instructions` and a JSON declaration in the input developer message. The
  operation union, user messages, attachments and server validation are unchanged. Other calls retain the configured
  schema ladder; Gemini and Chat Completions implement the preference as a no-op.
- Attachments: `{"type": "input_image", "image_url": "data:<mime>;base64,…", "detail": "high"}` and
  `{"type": "input_file", "filename", "file_data": "data:application/pdf;base64,…"}`.
- Stream: `response.output_text.delta` pieces (time to the first one = `first_token_ms`); the terminal event
  `response.completed` / `.incomplete` / `.failed` carries the full `Response`, which is parsed as before.
  An `error` event mid-stream → `network` (retryable).
- Reply: `output_text` (the joined deltas when empty); a `status == "failed"` response is classified from its `error.code`:
  `server_error` / `vector_store_timeout` → `network` (class `server_error`, retryable), `rate_limit_exceeded` →
  `rate_limited` (retryable), either one after partial output → class `stream_broken`; any other code (invalid prompt,
  image …) or none, a `refusal` content item, or an incomplete `content_filter` response raises `bad_response`; an incomplete `max_output_tokens` response with broken JSON also raises
  `bad_response`. Tokens come from the terminal response's `usage.input_tokens` / `usage.output_tokens`; the request id is
  the stream's `x-request-id` response header, falling back to the response `id`.
- Errors (`map_openai_error`): 401 → `auth_failed`; 403 → `permission_denied`; 404 or `model_not_found` → `model_not_found`;
  429 with a quota/credit/billing/spend code → `quota_exceeded` (not retryable, per the error-codes guide), otherwise
  `rate_limited` (Retry-After); 400/422 → `invalid_request`, or `unsupported_capability` when an image/file is "not supported";
  5xx → `network`; `APITimeoutError` → `timeout`; `APIConnectionError` → `network`. The error's request id
  (`err.request_id`, kept in `ai_calls.error`) comes from the `x-request-id` header, else from the message
  (see "Request ids").

## OpenAI-compatible (`OpenAICompatibleProvider`) — Chat Completions at `base_url`

- `client.chat.completions.create(model, messages=[system, …], stream=True, stream_options={"include_usage": True},
  max_completion_tokens=<output budget>,
  response_format={"type": "json_schema", "json_schema": {"name", "schema": <openai_strict dialect>, "strict": True}})`;
  images as `image_url` data URLs. The output budget and compatible fallback are described under "Output limit"
  below. PDFs raise `unsupported_capability` before any request, so the intake falls back to manual entry.
  Structured output uses `json_schema_strict`: every object requires all properties and disallows additional
  properties; optional fields are nullable. Schema rejection is an explicit error, never an automatic downgrade.
  Models explicitly configured with `structured=false` still use JSON mode with the schema in the prompt.
- Deployments may explicitly set `PARTROUTE_AI_COMPATIBLE_NON_STRICT_CONFIG_IDS` to a JSON array of saved config IDs
  whose gateways have a verified strict-mode incompatibility. These IDs use the portable schema and `strict=false`
  in both the native request and the system prompt; schema rejection still fails without downgrading. All other
  configs retain strict mode. Restart API/workers together. This does not change credentials, model defaults, or
  server validation.
- Every compatible request includes the same converted schema once in the system instructions, including strict
  mode and repair requests, preserving user/assistant text and attachments. This supplements the wire contract
  and never replaces server validation or relaxes strict mode. Context estimates include both transmitted copies.
  SDK/HTTP regressions cover nested drawing configuration and coverage fields, as well as deployment-selected
  schema modes.
- Stream: `choices[0].delta.content` pieces are joined into the reply text and parsed as before; `delta.refusal` →
  `bad_response`; `finish_reason` from the chunk that carries it (`content_filter` → `bad_response`, `length` with broken
  JSON → `bad_response`). Usage = the last chunk carrying `usage` (with `include_usage` the final chunk has
  `choices: []`); none → tokens unknown (`None`, never 0). A server that rejects `stream_options` (400 naming it) is
  retried once without it and remembered per provider instance.
- Request id: the `x-request-id` response header of the stream, falling back to the chunk `id`.
- Same error mapping as OpenAI.

### Output limit (2026-10-17)

- Without an explicit output limit, a gateway applies its own default. The adapter requests the shared output
  budget for known models so that completion size follows the configured model limits.
- What the adapter sends: `max_completion_tokens` = the model's known output budget, reasoning included
  (`limits.output_token_limit`: `capabilities.max_output_tokens` if a configuration ever declares one, else
  `limits.MODEL_LIMIT_TABLE`, the table `assistant_context.model_limits` budgets input with): 65,536 for `gemini-2.5` / `gemini-3*`,
  128,000 for `gpt-5*` / `gpt-6*`. `max_completion_tokens` is tried first because OpenAI reasoning models reject
  `max_tokens` on Chat Completions. An unlisted model id gets no limit (unchanged behaviour): the table's highest value
  could exceed what a small local model accepts.
- Degrades like a rejected `stream_options`: a 400 naming `max_completion_tokens` → retried with `max_tokens`; a 400 naming
  that (or vLLM's "maximum context length … in the completion") → retried without a limit. The reached step is
  remembered per provider instance and noted in `StructuredResult.notes`. Concurrent calls identify the rejected
  parameter from their own request; synchronized transitions only advance this shared state. A late rejection
  retries the step already reached instead of failing or re-enabling a rejected parameter.
- The adapter does not send `reasoning_effort`; output limits are controlled by the shared model budget.
- A cut-off reply now says what happened: "model output was cut off at the token limit (8188 output tokens; no output limit
  sent, the server's default applied)" or "(…; max_completion_tokens=65536)", with the stream's request id. The
  same diagnostic covers a `length` completion with no text (reasoning can use the entire limit); reported usage
  and the header or chunk request id are retained. Missing usage remains explicitly unknown. The
  `ai_calls` row of a failed call still stores no token counts (the tracer records usage for successful calls only).
- Not covered: the Responses adapter sends no `max_output_tokens` (OpenAI's default is the model maximum); the
  tool_agent harness (main agent, Pydantic AI) sends no `max_tokens` either, so its gateway turns keep the gateway's
  default.

## Streaming and time limits (S1, 2026-10-11)

- Both OpenAI adapters always stream; `test_connection` streams too (the shared probe goes through
  `generate_structured`).
- Two limits per call:
  - idle timeout defaults to 60 s for short calls; caller budgets above 600 s allow up to 900 s of quiet reasoning,
    bounded by the request deadline. Explicit shorter idle overrides still win. Before the first chunk, the total
    request deadline applies. A stalled stream reports `timeout` with actual received progress;
  - total cap = the caller's `timeout_s`, over the whole call (request, wait for the first chunk, streaming, mode-ladder
    retries) → `timeout` ("exceeded the N s time limit" / "sent no response within"). Modeling and spec calls default
    to 900 s; the main turn defaults to 3600 s and synchronous spec extraction to 3300 s. Earlier explicit and frozen
    budgets remain authoritative. The tool runtime caps each model request at 900 s and retains the connection
    ceiling. Identity-bound 30 s heartbeats protect long work without extending its deadline; see
    `.trellis/spec/backend/long-cad-timeouts.md`. Other call-site defaults remain unchanged.
  - The SDK's `timeout=` stays `timeout_s`; it is the per-read HTTP timeout, a backstop for a silent socket.
- How: the request and the reads run on a daemon thread (`_pump`); the caller waits on a queue, so both limits fire on
  time even while a read is blocked or a gateway only sends SSE keep-alive comments. On any exit the stream is closed
  (releases the connection, stops generation).
- A stream that breaks after it opened is never a parse error on partial JSON: connection loss → `network` ("stream was
  interrupted", retryable); an SSE error event → `network` ("reported an error mid-stream", retryable); a stream that
  ends without a finish reason → `network` (retryable) unless the text is already a complete JSON object (kept, with a
  note); a stream with no data at all (empty body, only keep-alives or `[DONE]`) → `network` / `no_response`, retried by
  the existing bounded Tracer policy with every request recorded. No events plus an explicit non-SSE Content-Type
  instead reports non-retryable `unsupported_capability`; valid streamed events retain compatibility even under a
  mislabeled header. The request id of the stream is attached to all of them.
- Errors before the stream opens (HTTP status, connection) are unchanged and still drive the mode ladder and the
  temperature retry.
- `first_token_ms` (request sent → first text piece of the successful attempt) is on `StructuredResult` and stored in
  `ai_calls.response.first_token_ms` (JSONB, no migration).
- Known limit: a gateway can return a 504 before the model sends its first byte (mapped to `network`, retryable).
  Watch `first_token_ms` when investigating delays before the first streamed text.

## Agent executor tool_agent (`harness/tool_agent.py`) — Pydantic AI 2.55.0

- Only file that imports the framework. Model: `OpenAIChatModel(model_id, provider=OpenAIProvider(openai_client=AsyncOpenAI(
  api_key, base_url, max_retries=0, timeout, http_client=httpx2.AsyncClient(event_hooks))))` (no SDK retry: every request is
  one recorded call). Address, model id and key come
  from the adapter the server resolved from the role's frozen snapshot (`provider_for` → `bind_secret` / `bind_guard`);
  base URL and key are always passed, so `OPENAI_API_KEY` / `OPENAI_BASE_URL` are never read (the SDK still reads
  `OPENAI_ORG_ID` / `OPENAI_PROJECT_ID` if set, like the existing adapters). OpenAI and OpenAI-compatible configurations
  keep this Chat Completions transport. The shared `TOOL_FAMILIES` also includes native Gemini.
- Gemini uses `GoogleModel(model_id, provider=GoogleProvider(api_key, base_url, http_client,
  retry_options=HttpRetryOptions(attempts=1)))`; it never uses an OpenAI-compatible endpoint. The injected
  `httpx2.AsyncClient` has a finite timeout. The explicit key comes from the same frozen server provider, not the
  Google SDK's environment defaults. The framework's history keeps provider name `google` and native function
  calls/results/thought signatures; configuration and call ledger keep family `gemini`. One SDK attempt means one
  request and one recorded model call, including 429/503 failures.
- Every function tool and output tool sets `strict=False` (the framework turns strict on by default for OpenAI); checked
  offline: no `strict` key in the request. `parallel_tool_calls` comes from the profile options (default true);
  `tool_choice: "required"` is sent because the run ends with an output tool (`deliver` / `report_blocked`).
- Google's tool declarations use `functionDeclarations` and `toolConfig.functionCallingConfig`, with `strict=False`
  preventing the framework's automatic `VALIDATED` mode. Required output tools may use `ANY`. OpenAI's
  `parallel_tool_calls` is not a Google setting and is omitted from Google settings and request summaries. Observed
  parallel Google calls are reported by diagnosis; the harness still serializes writes and permits concurrent reads.
- Every model request is streamed (`agent.iter()` → `ModelRequestNode.stream`); OpenAI requests use `stream_options.include_usage`. The
  S1 limits apply around it (60 s idle after the first chunk, the attempt deadline overall). Usage comes from the
  response (`stream.response.usage`); 0/0 means not reported → tokens unknown (None), never 0. Request id: the
  `x-request-id` header (httpx2 response hook), else the chunk id (`provider_response_id`).
- Google streams `:streamGenerateContent?alt=sse`; usage comes from native `usageMetadata`, including thought tokens
  in output tokens, and native `responseId` supplies the fallback request id. Per-request observations are cleared;
  unsafe or secret-bearing request IDs are omitted. Unknown usage remains unknown.
- `provider.check_live()` runs before every model request (W1 live restrictions; the framework sends requests itself).
- Errors: bounded, cycle-safe cause traversal recovers `ProviderError`, native SDK errors, and transport failures
  before generic wrappers. OpenAI uses `map_openai_error`; Google uses `map_gemini_error` and retains native
  RetryInfo/HTTP classifications. A cause-less Google `ModelHTTPError` uses its status/body as a fallback.
  Constructor and protocol failures never echo a raw response or credential. A turn cut off after it was sent (timeout, cancel, broken
  stream) is stored at its worst case: estimated input tokens and the profile's token cap left (else 32,768 output).
- A response with `content_filter`, `length` or `error` finish reason is recorded with actual usage as a failed call
  and refused before any write or output tool runs. Prompt feedback blocks, invalid SSE and malformed function calls
  cannot produce a delivered result.
- The shared factory owns construction through iteration. It closes the Google SDK's async resources, its sync
  resources, and the supplied HTTP client (the SDK deliberately does not own the latter). OpenAI and its supplied
  HTTP client close in the same scope. Every acquired resource registers cleanup immediately; Agent/model
  construction, streaming errors, timeout and cancellation all close it. Cleanup failures are logged by exception
  type only and cannot skip other resources or replace the operation's primary result/error.
- Limits: `UsageLimits(request_limit=max_steps, tool_calls_limit)` (framework default 50 requests is never used);
  `max_total_tokens` is checked by the harness after each response was recorded — the framework's `total_tokens_limit`
  raises when the stream ends and the response that crossed it (already billed) would never reach `ai_calls`; the cost cap is the profile pricing's, checked by the runtime's `alive()`; `cost_limit` (genai-prices)
  is not used. Banner off (`pydantic_ai.BANNER_ENABLED = False`), instrumentation off (`agent.instrument = False`).
- Capability diagnosis (`diagnose`): 2–3 streamed requests with a throw-away tool; checks tool calling, two calls in one
  response, no `strict` in the request body (httpx2 request hook), usage fields, streaming. Only a passed tool-calling
  check turns `capabilities.tools` on (`POST /ai/model-configs/{id}/diagnose`). It uses the production factory and
  checks live credentials before every request. Google diagnosis verifies native declarations, the SSE endpoint,
  observed function calls and replayed function responses, rather than OpenAI-only wire fields. Version-bound
  `/probe` includes Gemini; only a passed current probe with tools makes its main-agent purpose ready. Transient
  failure leaves tools unknown instead of claiming they are unsupported.
- Tests inject `FunctionModel(stream_function=…)` through `tool_agent.model_factory`; the real OpenAI path was checked
  offline against an `httpx2.MockTransport` (stream, usage, header request id, 401 mapping with redaction).
  `tests/ai/test_tool_agent_transport.py` separately exercises the actual application factory, native Google SDK and
  MockTransport with a `.invalid` host, dummy key and deny-network guard. It covers every role output, two-request
  signature replay, errors/no retries, safety/length/protocol refusal, usage/budgets, live guards, cleanup and
  cancellation; the OpenAI native factory has its own transport regressions in that suite. FunctionModel remains
  a generic contract seam and is never evidence of native Gemini transport or model acceptance.
- **Real Gemini main Agent → 3D → 2D acceptance is deferred and has not run.** The user authorized implementation
  and offline verification while deferring real credentials/provider acceptance. No mock result proves CAD output
  from a real Gemini model.

## Request ids

- Success: the `x-request-id` header of the HTTP response (on the stream: `Stream.response.headers`).
- Failure: `APIStatusError.request_id` (the same header) when present. Otherwise, `request_id_from_error` checks
  structured error fields and message text, including "… (request id: <id>)". An extracted id that contains a piece
  of the configured key is dropped.

## Structured-output strategy (why)

One source schema (Pydantic: `AgentReply` embeds `app.cad.ops.Operation`; `IntakeResult`) is converted per provider by
`providers/schema_dialects.py`, and **every reply is validated with Pydantic on the server**. Provider-side schemas are
treated as guidance; only Pydantic validation decides.

- `gemini` / `portable`: the subset documented on `GenerateContentConfig.response_json_schema`. `oneOf` becomes `anyOf`
  (Pydantic's discriminated unions emit `oneOf` plus `discriminator`); `const` becomes a one-value `enum`, which is always required;
  `$ref` keeps no siblings; `default` / `title` / `discriminator` are removed (defaults are restated in `description`).
- Gemini alone moves variable-length array `maxItems` into an `at most N items` description. The verified native
  `gemini-3.8-flash` route rejected the 2.6 KB assembly skeleton schema with generic HTTP 400, while the same request
  succeeded after removing only variable-array upper bounds. Fixed lengths (`minItems == maxItems`), lower bounds,
  numeric bounds and references stay native. The original schemas and Pydantic validation retain every limit;
  `portable` and OpenAI strict conversion are unchanged. Google documents array bounds as supported, but warns that
  schema complexity can cause rejection ([structured output](https://ai.google.dev/gemini-api/docs/structured-output),
  checked 2026-10-18). This is a deterministic Gemini conversion, not a retry or a downgrade to JSON mode.
- `openai_strict`: strict mode requires every property in `required` and `additionalProperties: false`. So optional fields
  become nullable, where `null` means "use the default". Tuple `prefixItems` become `items`, and numeric/array bounds move into
  descriptions: support for them in strict mode is not confirmed, and an unsupported keyword fails the whole request.
  Consumers call `drop_nulls` before validating. This is lossless because every nullable field in our models is optional
  with default `None` (asserted by a test via `nullable_required_violations`).
- Fallback ladder when a server rejects the structured-output parameters (400 mentioning schema / response_format):
  OpenAI goes strict → non-strict `json_schema` → `json_object` (schema embedded in the instructions). Compatible servers
  retain the strict or explicitly selected portable mode and report schema rejection without downgrading; when configured for JSON mode,
  they may fall back from `json_object` to prompt only. Gemini goes `response_json_schema` → JSON mime type with the
  schema in the system prompt. The reached mode is remembered per provider instance and recorded in
  `StructuredResult.notes` / `structured_mode`.
  `capabilities.structured = false` starts the ladder at JSON mode.
- CAD Responses compatibility (verified 2026-10-17): with the same configured `gpt-6-astra`, modeling prompt and full
  `AgentReply` schema, a simple explicit box request returned an invalid empty-path `sweep` under strict decoding
  (13.4 s), but a valid `box` under JSON-object mode (11.0 s). The raw response already contained `sweep`; local schema
  conversion retained all 15 operation branches. This establishes a transport compatibility workaround for the tested
  endpoint, not that all models or native OpenAI endpoints have the same defect. The preference uses an instance-owned
  `ContextVar` and a call-local mode index: nested/concurrent CAD calls cannot change ordinary calls or the remembered
  fallback mode. It never mutates a model config, frozen snapshot, model ID, endpoint or credential. `Tracer` still
  records each actual `structured_mode`, token usage and retry attempt. This introduces no extra call or retry budget;
  structural validation remains mandatory, and source grounding, critical-field gates and independent STEP checks
  retain their existing execution-policy rules (including explicit Turbo skip records).
  Offline `test_modeling_transport.py` covers the SDK/HTTP boundary, create/modify/reconstruct, complete schema,
  isolation and rejection paths; those tests are separate from real exported-CAD acceptance.
- `temperature` is omitted unless given: Gemini 3 recommends the default, and some OpenAI reasoning models reject it.
  A 400 about temperature is retried once without it.
- These adapters do not use tool calling. The model returns a complete op list as structured output, and the backend runs
  the CAD kernel itself (deterministic "tool execution"), feeding kernel errors back in a repair turn. Native tool calling
  is used only by the agent executor tool_agent (section above), which requires `capabilities.tools` (set by the
  capability diagnosis or an administrator).

CAD requirement representation remains independent of transport. A `band` claims the final constant section, so a
nominal band across a source-stated chamfer can correctly fail even when the STEP geometry is correct. Modeling and
check-repair prompts teach complete material `prism` requirements plus explicit hole-entry `countersink` requirements
for plain bores with equal-leg mouth chamfers; both mouths need separate opposing entries. Outer turned transitions
can use a complete `revolve` section. Dimensions must be derived from source sizes, never from STEP measurements;
neither removing the chamfer nor trimming away undescribed end material is a repair. The saved stepped-plate regression
in `tests/ai/test_chamfer_requirements.py` checks the same STEP with the old and correct requirements, full material
accounting, and rejection of missing/wrong chamfers and ungrounded dimensions. It does not prove a live model will obey
the guidance; real modeling and exported-STEP acceptance must still be checked separately.

## Needs a real key to verify

- Gemini accepts the full `AgentReply` schema in `response_json_schema` (complexity limits are not documented; fallback exists).
- OpenAI strict mode accepts the `openai_strict` schema for the configured model.
- Exact error bodies: Gemini invalid key (400 API_KEY_INVALID vs 401), and OpenAI quota codes.
- Vision probe answers, PDF intake quality, token / latency figures.
- Streaming against the configured compatible endpoint: long spec extraction (time, tokens, `first_token_ms`);
  support for `stream_options.include_usage` and usage on the last chunk; request-id extraction from its error bodies.
