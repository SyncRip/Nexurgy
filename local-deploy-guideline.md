# Nexurgy Local Deployment Guideline

Nexurgy brings engineering requests, CAD models, drawings, manufacturing estimates, and their execution records into one project workspace. This guide describes the local Docker version and common ways to use it. It focuses on the work you want to complete, the inputs it needs, and how to judge the result.

The web application, API, database, geometry kernel, and file storage run in local containers. Real AI execution uses the model provider you configure and sends relevant request content to that provider. The supplied example configuration works without provider credentials by using explicitly labelled simulated AI.

## Start locally

From the repository root, with Docker running in Linux-container mode:

```sh
docker compose --env-file .env.example up --build -d --wait --wait-timeout 180
```

Open [http://localhost:8080](http://localhost:8080). Use an example account from the [local setup README](README.md#start-the-local-demo) and the password in your local configuration. Select a project before creating or importing engineering work. Administrator and project-management permissions are needed for configuration changes; other project roles have narrower actions.

The first build downloads dependencies and compiles native CAD tools. The `initialize` service is expected to exit successfully after database migrations and demo seeding; the other services continue running. Detailed requirements, private configuration, and operating commands are in [README.md](README.md).

The interface and engineering requests support English and Chinese. Changing the interface language does not translate an uploaded source document or change the engineering meaning of an existing result.

## Understand the local defaults

| Setting | What to expect |
| --- | --- |
| Example data | Seeded projects, accounts, and manufacturing rates are synthetic. |
| Simulated AI | The no-key example setup uses deterministic responders for supported demonstrations. It does not measure a real model's reasoning or performance. |
| Execution policy | New projects use Turbo. Read the policy recorded on each run because saved project settings and historical runs may differ. |
| Engineering checks | Turbo can record checks as skipped or outputs as unverified. A completed execution does not establish engineering approval. |
| Feature availability | Collaboration, project tasks, stages, process modules, the public template library, and new per-request model/strategy choices are frozen by default. Available controls also depend on permissions. |

Project workflow canvases and project templates remain part of the core workflow. A fresh project can have an empty saved-template list; create a workflow from an available template or a canvas when you need one.

For real AI, use the private configuration described in the README, disable simulation fallback, and configure an available provider and model. New model forms start without a model ID or custom endpoint. Enter an actual model ID available to your account; compatible services require an endpoint you supply. Assign the configuration to the relevant purposes, such as modeling or planning, and wait until model detection reports it ready before starting a new execution. Existing executions retain their frozen configuration.

The example Compose configuration passes a defined set of provider variables to both the API and worker. An environment-variable name entered in a model form must exist in both services. A custom name alone does not supply a credential. The optional server-managed direct-key workflow also requires a privately configured encryption keyring. Keep credential values out of source files, build arguments, and shared examples.

## Find the right workspace

| Area | Use it for |
| --- | --- |
| Workbench | Start a request, select ongoing work, and review items needing attention. |
| Parts | Work with individual part revisions and assemblies: models, drawings, process plans, quotes, files, and history. |
| Workflows | Inspect runs, create reusable project templates, and configure the engineering team. |
| Resources | Organize project files and generated engineering materials, inspect versions, and locate reusable inputs. |

The assistant can help turn a request into engineering actions. Its role labels describe responsibilities such as project manager, designer, modeler, assembler, drafter, process engineer, and quoter. Use saved revisions, run steps, and output records to establish what actually happened. Team instructions and reusable text skills guide behavior; they do not replace geometry checks or change computed prices.

## Provide a clear request and the right context

State the intended object, units, essential dimensions, material when relevant, relationships between parts, and required deliverables. Identify constraints that must remain fixed. Separate known facts from choices the assistant may make.

For an assembly, describe its hierarchy, quantities, interfaces, and positioning constraints. If source documents disagree, identify the conflict and ask for clarification rather than silently selecting a value.

Uploading a file stores a project asset. To use it in reasoning or modeling, reference its intended version in the conversation or connect it to the relevant workflow input. Select the target part or revision for modifications. If a modeling action requires a part, create or select that part before continuing.

A reusable request structure is:

```text
Goal: What should be created or changed?
Units and dimensions: Which values are authoritative?
Source context: Which attached file or saved revision should be used?
Constraints: What must stay unchanged, and what remains unknown?
Deliverables: Which models, drawings, estimates, or export files are needed?
```

### Choose inputs by purpose

| Input | Appropriate use and boundary |
| --- | --- |
| Written requirements | Define a part, request a design, or begin a structured specification. Complex descriptions benefit from a clarification stage before geometry. |
| Single-solid STEP | Import an exact model into a part. It becomes an imported base for supported editing; the original CAD feature history is not recovered. |
| Structured assembly STEP | Use the assembly import route to retain hierarchy, repeated definitions, and instance positions. Imported positions are fixed; original design mates are not restored. |
| DXF or DWG | Use drawing evidence for reconstruction, or an explicit profile-import workflow. Profile import needs a defined profile layer, units/scale, and thickness. |
| PDF or raster drawing | Use drawing intake to organize manufacturing information. The CAD drawing-to-STEP reconstruction route specifically accepts DXF/DWG. |
| STL, OBJ, or GLB | Use as mesh references and previews. A reference mesh does not provide an editable exact solid or a STEP deliverable. |

The local application upload limit is 50 MB per file. Large or unsupported assemblies can require simplification or separate subassemblies; inspect import diagnostics before using them downstream.

## Common use cases

### 1. Create a first part and revise it

Start with a simple object whose geometry is fully stated. Create or select a part and use a model-creation action or a workflow with a text input feeding a model step. In the default no-key setup, ordinary simulated assistant chat can suggest an action without creating a model itself. The modeling simulator supports this exercise:

> Create a rectangular block 60 x 40 x 10 mm with no holes. Save a model revision and provide STEP and GLB files.

Inspect the saved revision, dimensions, and downloadable files. The saved model establishes the context for a subsequent request:

> Using this revision, change the overall size to 70 x 40 x 10 mm. Keep it as a solid block with no holes and save a new revision.

The expected result is a new version with the earlier revision still available. Check which revision is current before creating drawings or estimates. Simulated responses support a limited set of requests; arbitrary free-form design needs a usable real model.

### 2. Explore a simple product concept

Use a configured real model for a concept that needs design choices, such as a simplified toy vehicle:

> Create a simple toy car with a rectangular body and four cylindrical wheels. Use millimeters, keep the geometry simple, and provide a STEP file. Record assumptions about dimensions and wheel placement.

This is an exploratory CAD concept. Review the proposed design and any assumptions before accepting it. A provider may need clarification, return an unsupported operation, or require another attempt. A generated preview and STEP file do not establish that wheels rotate, parts can be manufactured, or the design is verified.

Keep the initial scope small. Add detail through later revisions after the basic shape is satisfactory.

### 3. Continue from an existing STEP model

Upload the model, then explicitly import it into the appropriate part or assembly workspace. A single-part import expects one valid solid; an assembly belongs in the assembly import route.

For a part, inspect the imported base and request a supported change against that exact revision. For an assembly, inspect the hierarchy, repeated parts, BOM, and positions before adding constraints or producing drawings.

An existing shape does not supply missing material, tolerance, manufacturing intent, or original feature history. Add those facts explicitly when they matter.

### 4. Work from an engineering description

Begin a complex assembly with a specification and clarification pass:

> Organize the attached description into a part hierarchy, quantities, interfaces, known dimensions, and unresolved questions. Cite the relevant source information. Identify conflicting dimensions before generating geometry.

Reference the intended description version. Confirm ambiguous quantities, thicknesses, hole definitions, and placement relationships. Build well-defined parts or subassemblies first, then review their relationships and coverage.

The useful first result may be a specification, questions, or a partial set of valid parts. Treat a simplified or Turbo-generated candidate as exploratory while source conflicts remain unresolved. A complete, verified industrial assembly is not a guaranteed one-request outcome.

### 5. Reconstruct a customer CAD drawing

Supply an authentic DXF/DWG, identify the intended sheet when needed, and use a real model configuration:

> Reconstruct the selected drawing sheet as a 3D part. Use the stated units and dimensions, report conflicts, and ask for missing critical information. Save the resulting revision with its source references.

The reconstruction flow uses drawing evidence and may ask about units, scale, conflicting dimensions, or missing geometry. Unsupported features can prevent a valid result.

This route is unavailable in no-key simulation: the deterministic responder cannot read drawings, so it reports `simulation_unsupported`. PDF/image intake and explicit profile import are separate workflows with different outputs.

### 6. Generate drawings and package a delivery

Start from a saved exact model or assembly revision. Generate drawings against that version, review the available pages and checks, and download the requested formats or delivery package.

For a reusable part workflow, a useful pattern is:

```text
Model input -> Drawing -> Export
```

Connect the model to the export as well when model formats are required. A drawing input alone is not a substitute for every model-format input. Inspect the actual package contents for STEP, GLB, SVG, DXF, and DWG as applicable; a package can be partial.

Where supported, run explicit checks on the exact saved drawing or model. A successful DWG conversion or independent readback is useful evidence, but it does not establish that every external CAD editor can open, edit, and resave the file without issues.

### 7. Estimate manufacturing work and compare a change

Use a saved model revision or drawing intake as the source of a process plan. Supply a supported material, quantities, and commercial inputs, then create the associated draft quote. Suitable project templates include Design to quote, Customer drawing to quote, and One request to manufacturing estimates.

An example objective is:

> For this revision in AL6061-T651, prepare a machining plan and estimate quantities of 1, 10, and 50. State assumptions, missing inputs, and exclusions.

The default catalog also includes AL7075-T651, S45C, SUS304, and POM. These calculations use synthetic example rates. Purchased parts, assembly labor, and missing prices need separate treatment; blocking gaps can prevent pricing. Drawing-derived quantities can be estimates rather than exact geometry facts.

After a design change, regenerate the affected drawings and recalculate the plans and quotes you intend to use. Earlier artifacts remain associated with their original sources and may be stale.

## Reuse workflows and interpret their results

A workflow makes the intended inputs, processing steps, reviews, and outputs explicit. Use a saved template for repeatable work, but supply the fresh part, revision, or files for each new run. A template or completed-run-derived draft is not a copy of the prior run's engineering approval.

Typical patterns are:

| Pattern | Purpose |
| --- | --- |
| Text -> Model -> Drawing -> Export | Turn a scoped request into saved CAD and drawing artifacts. |
| Model input -> Drawing -> Export | Produce outputs from a known model revision. |
| Model or drawing intake -> Process plan -> Quote | Create a traceable manufacturing estimate. |
| Description -> Specification -> Assembly -> Drawings -> Delivery | Organize a larger engineering request into staged results; real-model and coverage limits still apply. |

Templates can include human-review or clarification steps. A run waiting at one of those steps needs the requested decision before it can continue. Closing a browser tab does not stop the background worker.

Read three things separately: **execution state, available artifacts, and engineering verification**.

| State or label | How to interpret it |
| --- | --- |
| Queued / running | Work is pending or executing; check progress before submitting another request. |
| Waiting / needs input | A clarification, review, or decision is required. |
| Succeeded / done | The execution finished; inspect the saved outputs and their checks. |
| Partial | Some requested results may be missing. Review coverage and the actual delivery contents. |
| Failed | Inspect the failing step, diagnostics, and any outputs already saved before retrying. |
| Unverified / skipped check | The relevant engineering check has not passed through the required validation path. Turbo can produce this state intentionally. |

Standard and Turbo are execution policies, separate from whether AI is simulated or real. Standard keeps the configured business/engineering gates; Turbo can skip them and records that fact. Use the controls available in the local build and read the policy saved with the result. Explicit later checks add evidence without rewriting the original execution history.

## Recover from common interruptions

| Situation | Useful next action |
| --- | --- |
| The browser disconnects or a request times out | Reopen the existing run and inspect saved revisions before creating a new request. Use the available retry/resume action when appropriate. |
| Work remains queued | Check the worker service and run diagnostics. Zero model calls does not mean a model finished. |
| A request has no target part | Select or create the intended part, then continue the modeling action. |
| A file is in Resources but was not used | Reference its exact version in the conversation or bind it to the workflow input. |
| A fresh project has no saved templates | Create a project workflow from an available template or the canvas. The public library is a separate, frozen capability. |
| A provider is not ready | Check private credentials, endpoint, model availability, purpose assignment, and the run's frozen configuration. Real-provider readiness may remain unavailable in the no-key simulated demo. |
| A drawing reconstruction reports `simulation_unsupported` | Configure a real model for that route; simulation cannot substitute for reading the drawing. |
| A diagram or package is partial or unverified | Inspect its coverage, files, assumptions, and supported explicit checks before using it as a final result. |
| A feature is unavailable | Check permissions and the local feature defaults; some historical capabilities are intentionally frozen. |

Service diagnostics are available with:

```sh
docker compose --env-file .env.example ps --all
docker compose --env-file .env.example logs --tail=100 initialize api worker web
```

Use the same private `--env-file` as your installation when you customize configuration. Share only redacted diagnostics.

## Preserve data and share results accurately

Database records and uploaded/generated files use separate persistent Docker volumes. Keep them together when preserving an installation. The safe stop command keeps those volumes:

```sh
docker compose --env-file .env.example down
```

Removing the volumes removes the corresponding local records and files. Version history is not a substitute for a backup of the volumes.

When sharing a result, include the request context, source revision, execution policy, assumptions, check status, and actual deliverables. Label simulated AI and synthetic manufacturing data. Distinguish a previously saved result from a new live execution.

Keep real credentials and private customer data out of the repository, copied diagnostics, and shared examples. Review which documents and context will be sent before using a real provider. The local setup is for local use and review; a publicly accessible deployment is a separate deliverable.

## Repository references

- [Local setup and operating details](README.md)
- [Example configuration with blank provider credentials](.env.example)
- [Local service definitions](docker-compose.yml)
- [Built-in workflow definitions](server/app/workflow/templates.py)
- [Feature availability rules](server/app/services/feature_flags.py)
- [Drawing reconstruction behavior](server/app/services/ai_reconstruct.py)
