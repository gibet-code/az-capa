# Documentation

`az-capacity` is an Azure Functions application for collecting Azure capacity,
usage, reference, and operational data and presenting ODCR-oriented reports.

Documents are organized by purpose. A specification may describe current or
proposed behavior; its status section is authoritative.

## Start here

1. [Backend architecture](architecture/backend.md) describes the code layers and
   dependency direction.
2. [Application Scope](specs/app-scope.md) defines the subscription and location
   boundary applied to collection and user-visible data.
3. [Data Collection](specs/data-collection.md) explains how
   collection pipelines are scheduled, gated, and monitored.
4. [Azure Reference Data](specs/azure-reference-data.md) describes the Subscription
   and Location catalogues used by scope resolution and display enrichment.

## Architecture

- [Backend architecture](architecture/backend.md) - backend layers and ownership.
- [Frontend reporting architecture](architecture/frontend-reporting.md) - shared
  report model and visualization lifecycle.

## Feature Specifications

- [Application Scope](specs/app-scope.md) - enforced subscription and location
  boundary, diagnostics, and collection integration.
- [Authorization](specs/authorization.md) - application roles, trust boundaries, and
  endpoint policy.
- [Azure Reference Data](specs/azure-reference-data.md) - Subscription and
  Location catalogues, snapshots, and refresh lifecycle.
- [Business Context](specs/business-context.md) - subscription classification and
  report enrichment.
- [Data Collection](specs/data-collection.md) - centralized scheduling, run
  state, history, and administration API.
- [Activity Log History](specs/activity-log-history.md) - source collection,
  retained payloads, and inline operation assembly.
- [Global Filters](specs/global-filters.md) - shared report filters and their scope.
- [Frontend Preference Store](specs/frontend-preferences.md) - browser-side
  preference ownership and persistence.
- [Frontend loading-state convention](specs/frontend-loading-states.md) - shared SPA
  loading behavior.
- [ODCR Association Report](specs/odcr-coverage-report.md) - implemented coverage-report
  behavior and interactions.
- [ODCR coverage decisions](specs/odcr-coverage-decisions.md) - manual decision API and
  persistence contract.
- [ODCR usage views](specs/odcr-usage-views.md) - implemented reservation and
  reservation-group views.

## Operations

- [Install Azure Capacity in Azure](operations/installation.md) - complete
  first-time installation procedure and team handoffs.
- [Easy Auth setup](operations/easy-auth-setup.md) - Entra application and App Service
  Authentication configuration.
- [Environment variables](operations/environment-variables.md) - settings,
  defaults, and Bicep ownership.
- [Function package build and deployment](operations/function-package-deployment.md) -
  containerized test, package, and deployment workflow.

## Decisions and planning

- [ADR 0001: Interactive reporting stack](decisions/0001-interactive-reporting-stack.md)
  - accepted Alpine.js and Apache ECharts reporting stack.
- [Activity Log Materializers](planning/activity-log-materializers.md) - deferred
  domain histories and downstream projections.
- [Backlog](planning/backlog.md) - deferred work; it is not a statement of current
  architecture.

## Terminology

- **ODCR** - On-demand Capacity Reservation.
- **CR** - Capacity Reservation.
- **CRG** - Capacity Reservation Group.
- **App Scope** - the administrator-configured subscription and location boundary
  for this application instance.
- **Global Filters** - user-selected filters applied within App Scope and the
  signed-in user's Azure visibility.
