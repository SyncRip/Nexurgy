# Nexurgy GCP Demo Guideline

This guide introduces the hosted Nexurgy demo on Google Cloud Platform (GCP). You can explore the application in your browser, describe an engineering task through conversation, and download the resulting CAD files.

## Access the demo

Open the deployment URL provided in the presentation slides and sign in with the credentials supplied there. The slides are the reference for access details; this guide does not reproduce them.

After signing in, select **Demo machining Project** using the project switcher. This default demo project already has **Turbo mode enabled** and is the recommended starting point for the walkthrough below.

## Use Turbo mode

Use Turbo mode whenever practical for this demo. It allows the assistant to proceed with recorded assumptions when information is missing or conflicting, and skips engineering checks, reviews, approvals, and acceptance. This makes it useful for exploring a concept and working toward an initial deliverable. Generated results remain marked as not verified.

To enable Turbo for another project:

1. Select the project you want to use.
2. Open **Settings → Project**.
3. Turn on **Default Turbo** and confirm that the setting reads **On: new requests use Turbo**. The change saves automatically.
4. Return to the conversation and submit a new request.

The project owner can change this setting. If the switch is disabled for your account, use the supplied demo project or ask the project owner to enable it. Existing runs, retries, and their child operations retain the settings they started with; changing the project default applies to new requests.

A separate Turbo selector may not appear beside the conversation input. The project default still determines the mode for new requests. **Demo machining Project** is already configured, so you can proceed directly to the example.

## Basic example: create a toy car and export STEP

The recommended first task is to use the conversation directly in Turbo mode. With the demo project selected, enter this prompt:

```text
Create a simple toy car model from scratch and generate a STEP file.
```

Allow approximately **5–8 minutes** for this task. Completion time can vary with model response time, service load, and the work needed to produce the geometry and export.

Follow the progress shown in the conversation while Nexurgy works through the request. If it is still processing, check the current task before submitting the same prompt again.

When the task finishes, review the model preview and the saved result, then download the **STEP file** from the output. Confirm that the deliverable is available as an actual file. A preview or a message describing the model alone does not establish that the export is complete.

The car is an exploratory model. Turbo may choose dimensions, proportions, or component placement that you did not specify; review those assumptions alongside the result.

## Explore through conversation

You can also converse freely to develop your own concept, ask questions, and refine an existing model. Describe the intended shape, important dimensions, relationships between components, and the file you want at the end.

Open-ended conversation may require several prompts to reach the final form you have in mind. Keep each refinement clear and refer to the current model or saved revision so the assistant has the right context. For example:

> Make the body shorter and keep the four wheels in their current arrangement.

> Generate a STEP file for the latest revision of this model.

Review the outcome after each meaningful change and check that the final download corresponds to the version you want. For broader examples and explanations of supported inputs and result states, see the [local deployment guideline](local-deploy-guideline.md); its installation instructions apply to the local version.
