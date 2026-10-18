# Nexurgy

Nexurgy is an engineering workspace that connects design requirements, CAD models, drawings, manufacturing plans, and cost estimates. It brings these activities into a shared project context, with saved revisions, reusable workflows, and traceable execution records.

This repository provides the application and Docker configuration for a complete local installation. The interface supports English and Chinese.

## What you can do

- **Develop engineering requirements:** organize written requests and source files, clarify constraints, and prepare structured specifications.
- **Create and manage CAD models:** work with supported modeling and import workflows for parts and assemblies, and retain revision history.
- **Prepare drawings and deliverables:** generate engineering drawings and assemble export packages linked to their source models.
- **Explore manufacturing options:** develop process plans and preliminary cost estimates using project inputs and configured rates.
- **Reuse engineering workflows:** connect activities, run them in the background, and review progress, outputs, and items needing attention.

Choose the guide for your environment:

- [Local deployment guideline](local-deploy-guideline.md): local setup, common use cases, supported inputs, and result interpretation.
- [GCP demo guideline](gcp-deploy-guideline.md): hosted demo access, Turbo mode, and a first modeling task.

## How the local version works

The browser workspace is backed by an API, a background worker, a CAD runtime, a database, and file storage. Docker Compose builds and connects these components, initializes the application, and exposes a single local web address.

| Location | Purpose |
| --- | --- |
| [web/](web/) | Browser interface and local web serving. |
| [server/](server/) | Application services, engineering operations, background execution, and data management. |
| [docker-compose.yml](docker-compose.yml) | Local service configuration and persistent storage. |

The supplied configuration uses synthetic demo data and explicitly labelled simulated AI, so you can explore supported demo workflows without provider credentials. Real AI execution requires a configured provider and sends relevant request content to that provider.

## Start the local demo

Use Docker Desktop with Linux containers, or Docker Engine with Docker Compose v2.20 or newer. The backend targets Linux amd64; ARM hosts require emulation. The first build needs internet access, disk space for the CAD dependencies, and time to compile native tools.

From the repository root:

```sh
docker compose --env-file .env.example up --build -d --wait --wait-timeout 180
```

Open [http://localhost:8080](http://localhost:8080). Sign in with an example account listed in the [demo account definitions](server/app/seed.py) and the `NEXURGY_DEMO_PASSWORD` value from [.env.example](.env.example). Select the sample project to begin.

The `initialize` container exits successfully after preparing the database and demo data. The remaining services continue running.

## Configuration and data

[.env.example](.env.example) contains the available local settings and fictional defaults. For a private setup, copy it to `.env`, adjust the values, and use `--env-file .env` consistently in Compose commands. Keep real credentials in private runtime configuration.

To use real AI, disable simulation in your private configuration, supply provider credentials, and configure a ready model in the application. Saving credentials directly through the interface also requires a private encryption keyring. Feature availability depends on the local configuration and project permissions; the [local deployment guideline](local-deploy-guideline.md) explains the default boundaries.

Database records, uploaded files, and generated artifacts are kept in persistent Docker volumes. To stop the installation while retaining that data:

```sh
docker compose --env-file .env.example down
```

Preserve both the database and file-storage volumes when backing up an installation. Use your selected environment file for subsequent starts and stops.

## Review engineering results

Use saved revisions, actual downloads, and check records to assess a result. A completed workflow or visible preview does not establish engineering approval. Some checks may be skipped under the default Turbo policy, and the demo manufacturing rates are synthetic. Review geometry, drawings, assumptions, and estimates before using them for production decisions.
