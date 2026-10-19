# Nexurgy: AI Builder Cup Judges' Guide

Nexurgy connects an engineering conversation to saved CAD parts, assemblies, drawings, and downloadable deliverables. This guide follows the supplied screenshots to help you review the toy car demonstration in **CarDemo**. You can also test the exact demonstration prompt yourself within the same project.

## 1. Sign in

Open the deployment URL provided in the presentation slides and use the sign-in credentials supplied there. Select **EN** if you prefer the English interface.

![Nexurgy sign-in page](<Screenshots/1. Login.png>)

*Figure 1. Sign in to the hosted application using the access details from the presentation slides.*

## 2. Start from the Workbench

The **Workbench** is the starting point for engineering requests and ongoing work. The main request box lets you describe a goal, while the cards below provide access to existing parts and recent activity.

The left navigation connects the main areas: **Workbench** for requests, **Parts** for parts and assemblies, **Workflows** for execution records and reusable workflows, and **Resources** for project materials. The project selector at the top determines which project you are working in.

![Workbench showing the request box and existing project work](<Screenshots/2. Workbench.png>)

*Figure 2. The Workbench combines a conversation entry point with saved engineering work.*

## 3. Select the prepared CarDemo project

Open the project selector near the Nexurgy name and choose **CarDemo**, as highlighted below. This project contains the toy car example used in the following screenshots.

Keep **CarDemo** selected when viewing the demonstration or testing its prompt yourself.

![Project selector with CarDemo selected](<Screenshots/3. SwitchToDemoProject.png>)

*Figure 3. Select CarDemo to follow the prepared toy car walkthrough.*

## 4. Open the saved conversation

From the Workbench, choose **Continue conversation** to browse previous chats. Open the CarDemo conversation whose current task matches the exact prompt used for the demonstration:

```text
Answer in English: Generate a complete toy car model assembly with several parts.
```

The conversation title may be shortened in the sidebar; use the **CarDemo** project label and the full current-task text to identify it.

![Annotated Workbench showing where to start a request or continue a conversation](<Screenshots/4. Start Chatting OR Check Previous Chats.png>)

*Figure 4. Start a new conversation or use Continue conversation to inspect an existing one.*

## 5. Review the generated engineering results

### Assembly result

The prepared conversation shows an **Assembly generation workflow** and a saved **Toy Car Assembly** result. Its result card includes a model preview, a **View 3D assembly** action, and expandable sections for saved files, check details, and version/source information.

Use **View results** to locate the result area, then **View 3D assembly** to open the assembly workspace. The visible **Turbo**, **project default**, and **Done · not verified** labels describe how this example was executed.

![Saved toy car assembly result in the conversation](<Screenshots/5_1. Demo Workflow 1.png>)

*Figure 5. The conversation links the original request to a saved assembly and its engineering records.*

### Component models and drawings

Scroll through the result area to inspect the component previews. The prepared example uses three part definitions with seven total instances:

| Component | Instances in the assembly |
| --- | --- |
| Toy Car Chassis | 1 |
| Axle Shaft | 2 |
| Toy Wheel | 4 |

Repeated wheels and axles reuse their corresponding part definitions. The drawing previews below the models show a general arrangement and three component overview pages, with references to their source assembly revision.

![Component model previews and four drawing previews](<Screenshots/5_2. Demo Workflow 2.png>)

*Figure 6. The result connects reusable component models to an assembly drawing set.*

### Delivery summary

The **Delivery summary** brings together the engineering specification, component models, assembly, and drawing/delivery references. Use it to understand the scope of the completed task, then inspect the linked saved results and available downloads.

For the drawing and export details, expand **Parts, drawings and delivery details**. Check the actual files and the selected revision when assessing a deliverable.

![Delivery summary for the prepared toy car example](<Screenshots/5_3. Demo Workflow 3.png>)

*Figure 7. The delivery summary organizes the work completed during the conversation.*

## 6. Inspect parts, the assembly, and STEP exports

Open **Parts** in the left navigation. The **Parts** tab lists the individual component models, including their names, materials, revision labels, and previews. The **Assemblies** tab provides access to the assembled result.

A component card labelled **Not run** with **No workflow run yet** refers to that part's workflow history. The prepared example still has saved component model revisions, as shown in the conversation results.

![Parts workspace showing the chassis, wheel, and axle models](<Screenshots/6_1. View Parts.png>)

*Figure 8. Browse the saved component models or switch to Assemblies.*

Open **Toy Car Assembly** to inspect its instance tree and 3D view. The prepared example lists one chassis, front and rear axles, and four wheel instances. The workspace also provides **Mates**, **BOM**, **Checks**, **Drawings**, and **History** tabs for further inspection.

Use **Assembly STEP** to download the assembly model, or **Part STEP set** to download the component STEP files. Confirm that the files correspond to the selected assembly revision. These controls provide a way to inspect the CAD deliverables beyond the browser preview.

![Toy car assembly workspace with instance tree and STEP download controls](<Screenshots/6_2. View Assembly.png>)

*Figure 9. Inspect the assembly structure and access assembly or component STEP exports.*

## 7. Test the same demo prompt in CarDemo

You are welcome to test the demonstration prompt yourself. Keep **CarDemo** selected and start a **New conversation**, or enter the request through the Workbench's **Start with a request** box.

Use **Turbo mode**, as shown in the prepared demonstration. (btw: You are already in Tubo mode if you select **CarDemo** Project) To confirm the project setting, open **Settings → Project** and check that **Default Turbo** reads **On: new requests use Turbo**. The project owner can enable the switch if needed; it saves automatically and applies to new requests.

Copy and submit this exact prompt:
```text
Answer in English: Generate a complete toy car model assembly with several parts.
```

Keep the wording, capitalization, and punctuation exactly as shown, without appending additional instructions for this demo test. It is the same prompt used in the saved CarDemo demonstration.

Follow the task's progress in the conversation. When it finishes, inspect the saved assembly, component models, drawing results, and available downloads using the steps above. Processing time and generated design details can vary between runs.
