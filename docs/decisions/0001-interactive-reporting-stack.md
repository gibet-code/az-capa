# ADR 0001: Interactive reporting stack

## Status

Accepted and implemented.

## Context

The no-build Alpine.js frontend needs coordinated report charts, compact report
tables, and filtered detail tables. Report interactions must preserve stable
reference values, cross-highlight summary visuals, cross-filter detail rows,
support keyboard users, and implement feature-specific domain calculations.

Apache ECharts 6 is already vendored and used by ODCR Usage. The application
already has established Alpine feature controllers, shared classic-script
modules, semantic tables, and asset-serving tests.

No evaluated library provides the complete required behavior without an
application-owned state and domain layer:

- Vega-Lite provides strong declarative selections and linked views but would
  duplicate the existing ECharts capability and still require integration with
  Alpine and detail tables.
- dc.js and Crossfilter emphasize conventional coordinated filtering, while the
  required stable-reference cross-highlighting is feature-specific. Crossfilter
  also has limited recent maintenance activity.
- AG Grid is powerful but heavier, and some advanced capabilities require an
  Enterprise license.
- Tabulator is suitable for conventional vanilla-JavaScript data grids but does
  not replace report domain state or specialized matrix semantics.
- FINOS Perspective targets a generic analytical workbench rather than this
  controlled operational workflow.

## Decision

- Retain Alpine.js and the classic-script, no-build frontend architecture.
- Standardize interactive report charts on the vendored Apache ECharts asset.
- Introduce a small framework-independent report model as the source of truth
  for reference population, focus population, grouping, facet selections, and
  renderer-neutral derived views.
- Keep feature domain calculations outside ECharts and outside DOM renderers.
- Use semantic HTML for compact report tables and matrices by default.
- Do not add Tabulator, AG Grid, Vega-Lite, Crossfilter, Perspective, or another
  dashboard framework for the initial Coverage Report implementation.
- Reconsider a grid library separately if a detail table demonstrates a
  concrete need for virtualization or substantially more table mechanics.

The implementation contract is defined in
[Frontend reporting architecture](../architecture/frontend-reporting.md).

## Consequences

Positive consequences:

- The application reuses an existing, proven chart dependency.
- Domain logic and report interactions can be unit tested without canvas or DOM
  rendering.
- Charts, semantic tables, and detail tables observe one coherent selection
  state.
- Features retain control over specialized operational semantics and
  accessibility.
- No framework migration, build tooling, or new commercial license is required.

Costs and constraints:

- The application must implement and maintain a small report model and adapter
  layer.
- Cross-highlighting and compound selection remain application behavior rather
  than library configuration.
- Compact report matrices require focused semantic HTML and CSS work.
- ECharts canvas charts require companion HTML controls and textual summaries
  for complete keyboard and assistive-technology access.

## Revisit triggers

Re-evaluate this decision when one of these conditions is demonstrated:

- Client-side report populations exceed the tested performance envelope of the
  report model.
- Multiple detail tables require virtualization, column pinning, editing, or
  grouping that the current shared table code cannot provide economically.
- Users need self-service pivoting, arbitrary visual composition, or authored
  dashboards rather than predefined operational reports.
- Maintaining equivalent chart interactions across several features becomes
  more expensive than adopting a declarative visualization system.
