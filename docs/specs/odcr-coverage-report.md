# ODCR Association Report

## Status

Current implemented frontend contract. The unresolved UX questions near the end
of this document are follow-up refinements, not prerequisites for the current
report.

This document records the current ODCR Coverage report and VM table behavior,
then specifies the report rework. It intentionally does not redesign the
existing page filters or backend API. Sections for those topics are retained as
placeholders for later work.

## Technical dependency

This feature uses the shared
[Frontend reporting architecture](../architecture/frontend-reporting.md) for its
report model, reference, focus, and visual populations, coordinated selections, ECharts
lifecycle, rendering boundaries, and shared accessibility mechanics. The stack
choice is recorded in
[ADR 0001: Interactive reporting stack](../decisions/0001-interactive-reporting-stack.md).

This document remains authoritative for ODCR domain calculations, visible
content, layout, feature-specific interactions, VM-table effects, and acceptance
criteria. If a shared default conflicts with an explicit requirement here, this
feature specification controls the Coverage Report behavior.

## Scope

In scope:

- The report-level Group by control and report-filter reset action.
- The six report tiles, their layout, metrics, tables, charts, and interactions.
- Shared report-table styling.
- Report-generated filtering of the VM table.
- Two VM table changes: a separate prerequisite-status column and coverage
  decision audit tooltip.
- Accessibility, empty states, loading states, and responsive behavior for the
  changed controls.

Out of scope for this rework:

- Changes to the existing global or VM filter controls.
- Changes to the ODCR Coverage backend API or its response schema.
- Changes to ODCR coverage decision editing or persistence.
- Changes to prerequisite evaluation, reservation-state computation, or
  Business Context resolution.

## Terminology

The UI currently presents several related but independent concepts. The rework
must not combine them into one status.

| Concept | Meaning | Current response source |
| --- | --- | --- |
| Coverage decision | Recorded as Required or Not required, or absent as Unmarked | `odcrCoverage.decision` |
| Coverage priority | Manual priority for Required VMs: High, Medium, or Low | `odcrCoverage.priority` |
| Prerequisite status | Automated supportability result: Supported, Unsupported, or Unknown | `odcrSupportabilityStatus` |
| Prerequisite reason | One automated check and its result | `odcrRequirements` |
| Reservation status | Actual backend-reported reservation association/allocation state | `reservationStatus` |
| Association | Assigned when reservation status is any value other than Not associated | Derived in the frontend |
| Dimension | Subscription, Location, or an enabled Business Context field used to group report data | Report Group by control |
| Report filter | A transient VM-table filter created by clicking a report row, column, cell, chart segment, or chart legend item | Frontend-only state |

The product-facing label for a configured organizational dimension remains its
Business Context field name. The generic word **Dimension** is used only when a
requirement applies equally to Subscription, Location, and Business Context
fields.

## Behavior before this rework

### Coverage report

The current report is a one-row, three-column region above the VM table:

1. **ODCR Association** summarizes VMs with and without a reservation
  association. It can be grouped by Subscription, Location, or an enabled
  Business Context field.
2. **Reservation status** is a custom CSS conic-gradient chart. It shows actual
   backend reservation statuses for the rows remaining after active report
   filters. Its center shows a VM count. It is not interactive.
3. **Unsupported ODCR configuration** lists failed prerequisite reasons and
   impacted-VM counts. It is sorted by count descending. Clicking a reason
   filters the VM table; Ctrl+click can select multiple reasons.

The current Group by picker belongs visually to the coverage matrix. It is not
presented as a shared control even though its choice affects the report's
dimension grouping. Changing it clears the matrix cell selection.

The report and VM table use the same already-loaded frontend row set. Existing
page filters and search determine the report's base population. Report-generated
filters then narrow the VM table and can affect other report tiles.

### VM table

- The **ODCR coverage** column shows the manual decision and, when applicable,
  its priority. An edit button opens the coverage editor.
- The decision's note, last editor, and last update time are only visible after
  opening the editor.
- The **Reservation status** badge color is also affected by prerequisite
  supportability. This visually mixes two independent states.
- Clicking an actionable Reservation status badge opens the prerequisite
  details drawer.
- There is no separate prerequisite-status column.

## Shared report controls

### Group by

One shared **Group by** picker appears once above the report grid. Its options,
in order, are:

1. None
2. Subscription
3. Location
4. Each enabled Business Context field, in backend-provided display order

The initial value is **None**. A saved user preference is not part of
this rework.

The selected dimension applies to every tile that supports dimension grouping:

- Coverage decision status by dimension
- ODCR Association
- ODCR Unassociation
- ODCR Prerequisites

ODCR Coverage Details and ODCR Prerequisite Issues do not change with the
dimension selection.

When Group by is not None, every dimension-aware tile appends **by
[Dimension]** to its base title and names its first table column **[Dimension]**
using the selected option's display label. For example, Subscription produces
**ODCR Association by Subscription**, **ODCR Unassociation by Subscription**,
**Coverage decision status by Subscription**, and **ODCR Prerequisites by
Subscription**, each with a **Subscription** first column. When Group by is
None, those tiles use their base titles and do not show a dimension column.
Report titles remain on one line; when the available tile width is insufficient,
the visible title uses an ellipsis and its native hover title exposes the
complete text.

Changing the dimension clears every active report-generated filter because
dimension-value selections cannot be translated reliably between dimensions.
It does not clear the existing page filters or text search.

### Clear report filters

A conditional clear-filter control is left-aligned above the report grid. The
**Group by** picker is right-aligned on the same row. On narrow screens, the
clear control occupies the first row and Group by remains right-aligned on a
second row.

- The clear control is hidden when no report-generated filter is active.
- When a report filter is active, it appears as one clickable control containing
  a revert icon, **Clear report filters.**, and an **Active filters:** list.
- The active list names selected facet types in selection order, not individual
  values. The Dimension facet uses the current Group by label. For example:
  **Active filters: Location, Prerequisite Status and Reservation Status**.
- Activating it clears report-generated filters from all tiles in one action.
- It does not clear existing page filters, text search, VM sorting, pagination,
  or page-filter state. Because it changes report filters, it clears all checked
  VM rows.
- After clearing, the control disappears and the VM table shows the population
  defined by existing page filters and search.
- Clearing report filters empties the Coverage category selection, returning all
  categories to their available, unhighlighted state. It does not change which
  block owns Unmarked.

The existing page-level action is renamed **Reset VM filters**. It remains with
the VM filter controls and retains its existing behavior. The distinct labels
make the scope of each action explicit:

- **Clear report filters** clears only filters created by report interactions.
- **Reset VM filters** clears the existing VM filter controls and search using
  the existing page behavior.

### Block coverage-category controls

The **ODCR Required** and **ODCR not Required** block headers expose the
coverage-decision categories that define their report populations. These are
interactive report-facet controls, not coverage-decision editing controls; they
never persist a new decision on a VM.

The ODCR Required header contains these coverage bullets in order:

1. **Required - High ([VM count])**
2. **Required - Medium ([VM count])**
3. **Required - Low ([VM count])**
4. **Unmarked ([VM count])**, when Unmarked is owned by this block

By default, no Coverage category facet is active. Every bullet is available and
none has the selected highlight. An empty selection means all categories are in
scope, matching the existing report-facet convention. The ODCR not Required
header always contains **Not required ([VM count])** as an available facet
value. It also contains **Unmarked ([VM count])** when Unmarked is owned by that
block.

- Each item uses the existing VM-table coverage bullet treatment followed by
  its label and count. There is no checkbox. The whole item is one target.
- A plain click on an available category makes it the sole selected Coverage
  category and applies the persistent selected highlight.
- A subsequent plain click on a different category replaces the current
  Coverage category selection with that value.
- Plain-clicking the sole selected category clears the facet and returns every
  category to the available, unhighlighted state.
- Ctrl+click on Windows/Linux or Command+click on macOS adds or removes one
  category without replacing the other selected values. Selected values are
  ORed.
- Keyboard Enter or Space performs the plain-click behavior. Ctrl+Space on
  Windows/Linux or Command+Space on macOS performs additive selection.
- Every change makes Coverage category the active editing facet, preserving all
  category alternatives and counts while every other report visual and the VM
  table apply the complete filter set.
- Selected state uses the same persistent highlight and `aria-pressed`
  treatment as other report facet controls. Availability is not represented as
  selection.
- Not required participates in selection like every other category. Its
  ownership is fixed to ODCR not Required and it cannot be moved.

The VM count beside a category reflects the current population after global
scope, page filters, text search, and all other active report facets. The
Coverage category facet itself is excluded from this count calculation so
deselected alternatives retain meaningful, recoverable counts. Counts are
recomputed after every applicable filter change.

Required priority is mandatory in the backend contract. As a defensive data-
quality fallback, a Required record whose priority is missing or is not High,
Medium, or Low belongs to **Required - Undefined** in the ODCR Required block.
This category and its facet are hidden when its count is zero, so they do not
surface for valid datasets. When present, it follows the standard Coverage
category selection behavior and ensures the record remains visible in report
totals.

When one or more values are selected, selected categories across both blocks
form one global Coverage category facet for the VM table:

`selected Required-block categories OR selected not-Required-block categories`

The category facet is then ANDed with Dimension, Association, Reservation
status, Prerequisite status, and other report facets. When the selection is
empty, the category facet is omitted and the VM table includes every category.

Within the report, each block projects the global category selection onto its
owned values:

- When the facet is empty, every report inside ODCR Required uses all categories
  owned by ODCR Required, and ODCR Unassociation uses all categories owned by
  ODCR not Required.
- When the facet is active, each block uses only selected values it owns. A
  block with no selected owned value shows its category-filtered empty state.
- Coverage decision status, which is outside both blocks, uses the global OR
  union applied to the VM table.

#### Move Unmarked

Unmarked belongs to exactly one block. A text action named **Move Unmarked
here** appears inline after the category bullets in the block that does not
currently own Unmarked. It is not right-aligned separately.

Activating the action atomically:

1. Moves the Unmarked bullet to the destination block.
2. Moves Unmarked's block population ownership to that block.
3. Moves **Move Unmarked here** to the other block.
4. Recomputes both blocks, the global Coverage category facet, and the VM table.

Moving Unmarked preserves whether it is part of the active selection. It changes
analytical ownership only and does not modify any VM coverage decision.

Unmarked ownership survives Group by changes, **Clear report filters**,
**Reset VM filters**, data refresh through the page's Refresh button, full page
reloads, and browser restarts. It is retained in identity-partitioned browser
`localStorage` and is not persisted by the backend. The default is ODCR
Required when no retained preference exists. Category selections do not
persist across a full page reload; Clear report filters resets the Coverage
category facet without changing ownership. See
[Frontend Preference Store](frontend-preferences.md).

The block title, bullets, counts, and move action share one inline flex row and
wrap together when space is insufficient. The action remains immediately after
the bullets rather than moving to a separate right-aligned region.

## Layout

At desktop widths, the report uses a two-row by three-column outer grid.
Coordinates are **column:row**. The resulting layout is:

| | Column 1 | Column 2 | Column 3 |
| --- | --- | --- | --- |
| Row 1 | ODCR Association | ODCR Prerequisites | ODCR Unassociation |
| Row 2 | ODCR Coverage Details | ODCR Prerequisite Issues | Coverage decision status by dimension |

The four reports in columns 1 and 2 form one bordered block titled **ODCR
Required**. This block uses the existing report-section background. ODCR
Unassociation forms a separate bordered block at 3:1 titled **ODCR not
Required**, using the same report-section background. Coverage decision status
occupies 3:2 without a block title or outer frame; its background matches the
immediately surrounding content surface. Gaps visibly separate the three blocks.

The ODCR not Required block extends from the top of the report to the ODCR
Required block's internal row divider. Coverage decision status begins exactly
at that divider, so its title baseline aligns with ODCR Coverage Details and
ODCR Prerequisite Issues. The horizontal block gap remains; there is no vertical
gap between the two right-column reports.

On narrow viewports, blocks become a single column. The ODCR Required block
collapses to one column in this reading order:

1. ODCR Association
2. ODCR Coverage Details
3. ODCR Prerequisites
4. ODCR Prerequisite Issues

The ODCR not Required block follows, then the unframed Coverage decision status
report.

The shared Group by and Clear report filters controls remain above the grid.
No tile is nested inside another tile.

## Shared table behavior and style

All report tables use the semantic-table approach and rendering boundaries in
the shared frontend reporting architecture. The following requirements define
the Coverage Report's feature-specific table presentation and interaction
contract.

- Text uses the normal foreground color. Headers and row labels are not
  uppercase and are not rendered as muted metadata.
- Numeric values are right aligned; labels are left aligned.
- Column headers remain visible while the table body scrolls.
- When a total footer is specified, it remains visible at the bottom while the
  body scrolls.
- The top-left corner and total-label cell remain visible where both horizontal
  and vertical scrolling are possible.
- Sortable headers use the same sort indicator and accessible `aria-sort`
  semantics as the main data tables.
- Every sortable header reserves space for its sort indicator. Header labels
  truncate with an ellipsis inside their own column instead of overlapping the
  indicator or adjacent columns, and a native title exposes the full label.
- The active sort is always visible. Sorting is stable, with the displayed label
  as the final ascending tie-breaker unless a tile says otherwise.
- A selected row, column, or cell has a persistent selected treatment that is
  distinguishable from hover and keyboard focus.
- Hovering or focusing a row label highlights its whole row.
- Hovering or focusing a column header highlights its whole column.
- Hovering or focusing a data cell highlights both its row and column; the
  intersection cell uses a slightly stronger shade.
- Hover is not the only way to expose information. Every mouse interaction has
  a keyboard equivalent and an accessible name.
- Empty and zero are different. Zero is displayed as `0` or `0%`; unavailable
  data is displayed as an em dash.
- Percentages use whole-number display precision. An absolute zero displays
  `0%`; a nonzero value below 1% displays `<1%`; all other values are rounded to
  the nearest whole percent. Sorting and calculations always use the unrounded
  numeric value, never the displayed text.
- The first column is a consistent, normal-weight row-label column in every
  report table. Its content is constrained to that column and never paints over
  adjacent measures. Long dimension values and reason labels use an ellipsis
  and expose the complete value through their native hover title.

## 1. Coverage decision status by dimension

### Purpose

Show how many VMs have an explicit manual coverage decision in each value of the
selected dimension.

### Table mode

Table mode is used when Group by is not None.

- One row represents one dimension value.
- The row label is the dimension value. Missing Business Context values use the
  existing **Not mapped** presentation.
- The single measure column is **Decision recorded**. It shows the percentage
  with an explicit Required or Not required decision followed by a muted
  `recorded / total` count, for example `72%  18 / 25`.
- A VM has a recorded decision when its coverage decision is Required or Not
  required.
- VMs with no decision are excluded from the recorded count but remain in the report base
  population.
- The default sort is Decision recorded descending, then dimension label ascending.
- The table can be sorted by dimension label or Decision recorded.
- Clicking a row applies that dimension value as a report filter to the VM
  table. Clicking the only selected row again clears that tile's selection.
- Dimension-row selection is shared with ODCR Coverage and ODCR Prerequisites.
  Additive selection uses the platform modifier behavior defined in
  Accessibility.

The title is **Coverage decision status by [Dimension]**, for example **Coverage
decision status by Subscription**. The generic title **Coverage decision status
by dimension** is used
only while data or dimension metadata is unavailable.

A compact overall pie appears to the left of the dimension table. It uses the
same Decision recorded and No decision categories as None mode and is not
grouped by the selected dimension. The pie is 108px square and its vertical
clickable legend appears below it. When the viewport is 1180px wide or narrower,
the compact pie is hidden and the dimension table uses the full tile width.

### None mode

When Group by is None, the table is replaced by an Apache ECharts pie chart with
two categories: Decision recorded and No decision. The pie is left-aligned and
its vertical clickable legend appears to the right. The center shows the
Decision recorded percentage.
Clicking a segment or its legend item filters the VM table to the corresponding
coverage-decision status.

## 2. ODCR Association

### Purpose

Show how many VMs are assigned to an ODCR, independently of manual coverage
decisions and prerequisite supportability. A VM is assigned when its
`reservationStatus` is any value other than `Not associated`.

### Table mode

Table mode is used when Group by is not None. The title is **ODCR Association
by [Dimension]**, and the first column uses the selected dimension label.

- One row represents one dimension value.
- Columns are **[Dimension]**, **Review**, and **Assigned**.
- Review is reserved for future review logic and displays an em dash.
- Assigned shows the percentage followed by a muted `assigned / total` VM
  count, matching Coverage decision status. Sorting uses the unrounded
  percentage value.
- The default sort is Assigned descending, assigned VM count descending, then
  dimension label ascending.
- Every column is sortable. The Review sort preserves the stable default order
  while review values are unavailable.
- Clicking a row filters the VM table to its dimension value. Shared dimension
  selection and additive selection follow the report-wide behavior.
- A compact Assigned versus Not associated pie appears beside the table. Its
  denominator is the tile's visual population and its center shows Assigned
  percentage.

### None mode

When Group by is None, the title is **ODCR Association** and the grouped table
is replaced by an Assigned versus Not associated pie with a vertical legend.
The center shows Assigned percentage. A separate **Review** indicator displays
an em dash until review logic is defined.

Clicking a pie segment or legend item filters the VM table to Assigned or Not
associated. Clicking the active selection again clears that facet.

## 3. ODCR Unassociation

### Purpose

Show the complementary percentage of VMs that are not assigned to an ODCR. A
VM is unassociated only when `reservationStatus` is exactly `Not associated`.

Grouped mode uses the same interaction and presentation pattern as ODCR
Association, with columns **[Dimension]** and **Unassociated**. Unassociated
shows the percentage followed by a muted `unassociated / total` VM count,
matching Coverage decision status. Sorting uses the unrounded percentage value.
There is no VMs or Review column and no Review indicator.

None mode shows a Not associated versus ODCR assigned pie with Not associated
as the primary segment and the unassociated percentage in the center. Segment,
legend, and dimension-row interactions use the shared Association and Dimension
facets. Association and unassociation percentages are complementary for the
same visual population.

## 4. ODCR Coverage Details

### Purpose

Show the actual reservation statuses exactly as reported by the backend,
without collapsing individual statuses into the association summary.

### Chart

- The tile is always an Apache ECharts pie chart.
- It uses the same ECharts asset, palette conventions, tooltip styling, resize
  handling, and canvas renderer as the ODCR Usage chart.
- Each distinct backend `reservationStatus` value is one segment. The frontend
  does not rename, merge, or infer statuses for this chart.
- The legend shows status, VM count, and percentage and is ordered by count
  descending, then status label ascending.
- The center is empty. Percentages remain available in the legend, tooltip, and
  accessible textual summary.
- The pie is compact and appears to the left of a semantic report table. The
  table has **Reservation status** and **VMs** columns, follows the same header, row,
  border, scrolling, and selection styles as the other report tables, and uses
  one row per pie segment.
- The VMs column is wide enough to display its complete label and sort indicator.
  Its header is right-aligned with the counts below it.
- The table defaults to VMs descending, then reservation status ascending. Both
  columns are sortable.
- Each Reservation status label starts with a small circular marker using the
  exact color of its corresponding pie segment.
- When the viewport is 1180px wide or narrower, the compact pie is hidden and
  the reservation-status table uses the full tile width.
- Clicking a segment or legend item filters the VM table to that exact backend
  reservation status. Clicking the active item again clears that tile's filter.
- This tile is not affected by Group by. It is still affected by existing page
  filters and by report filters from other tiles. Its own Reservation status
  selection remains visible but does not reduce the statuses available here.

## 5. ODCR Prerequisites

### Purpose

Show prerequisite supportability by the selected dimension, separately from
reservation status and manual coverage decisions.

In table mode the title is **ODCR Prerequisites by [Dimension]** and the first
column is named for that dimension. In None mode the title is **ODCR
Prerequisites**.

### Table mode

Table mode is used when Group by is not None.

- One row represents one dimension value.
- The table has the selected dimension and **Unsup. or unkn. VMs** columns.
  The abbreviated header has the native title **Unsupported or unknown VMs**.
- Unsup. or unkn. VMs is the count of VMs whose summary prerequisite status is
  Unsupported or Unknown. No percentage or total footer is shown.
- The default sort is Unsup. or unkn. VMs descending, then dimension label
  ascending. Both displayed columns are sortable.
- Clicking a row filters the VM table to that dimension value.
- A compact overall pie is aligned to the top-left of the table and has two
  segments: Supported and Unsupported or unknown. A vertical clickable legend
  sits below the pie. Supported selects the Supported summary status;
  Unsupported or unknown selects Unsupported and Unknown together.
- The pie is 108px square and its center shows the Supported percentage.
- At 1180px and below, the compact pie and legend are hidden and the table uses
  the full tile width.
- The previous Hide fully supported rows toggle is removed.

### None mode

When Group by is None, the table is replaced by the same two-segment Apache
ECharts pie used in grouped mode: Supported and Unsupported or unknown. The pie
is left-aligned with a vertical clickable legend immediately on its right. The
center shows the Supported percentage. Supported filters to that exact summary
status; Unsupported or unknown selects Unsupported and Unknown together.

## 6. ODCR Prerequisite Issues

### Purpose

Show which individual ODCR prerequisites have Unsupported or Unknown VM results.

### Table

- One row represents one prerequisite from
  `metadata.odcrRequirements.definitions`.
- Columns are Prerequisite and **Unsup. or unkn. VMs**. The abbreviated count
  header has the native title **Unsupported or unknown VMs**.
- Unsupported or unknown VMs is the distinct count of VMs for which that
  prerequisite's result is Failed or Unknown.
- A VM logically has exactly one status for every prerequisite. The response is
  sparse: Passed results are omitted from each row and are reconstructed using
  `metadata.odcrRequirements.defaultStatus`; Failed and Unknown results are
  returned explicitly in `odcrRequirements`.
- Passed results do not contribute to the displayed count.
- No percentage or grand total is shown. One VM can have a Failed or Unknown
  result for multiple prerequisites, so row counts are not mutually exclusive
  and their sum can exceed the number of affected VMs.
- The default sort is Unsupported or unknown VMs descending, then Prerequisite
  ascending.
- The table can be sorted by Prerequisite or Unsupported or unknown VMs.
- Clicking a row filters the VM table to VMs whose result for that specific
  prerequisite is Failed or Unknown. Clicking the only selected row again
  clears that tile's filter.
- Ctrl+click on Windows/Linux or Command+click on macOS selects or clears
  additional prerequisite rows. Selected prerequisites are ORed: a VM is shown
  when at least one selected prerequisite has a Failed or Unknown result.
- Rows with a zero count are not displayed.
- This tile is not affected by Group by. Selecting None does not change its
  presentation or behavior; it remains the same table.

The VM-level summary prerequisite status is derived independently from these
per-prerequisite results using this precedence:

1. If at least one prerequisite is Failed, the summary is Unsupported.
2. Otherwise, if at least one prerequisite is Unknown, the summary is Unknown.
3. Otherwise, the summary is Supported.

The combined count in this table is not a count of summary Unsupported VMs. A VM
whose summary is Unsupported can also contribute an Unknown result to another
prerequisite row.

## Pie chart standard

Every pie chart in this report uses Apache ECharts, already vendored for the
ODCR Usage chart, through the shared ECharts adapter defined by the frontend
reporting architecture. The previous CSS conic-gradient generator is discarded.

- Labels and legend values come from the same data model used for filtering.
- Tooltips show label, absolute VM count, and percentage.
- A center label shows the tile's primary percentage only where the tile
  specification defines one. ODCR Coverage Details intentionally has an empty
  center. Every chart has an accessible textual summary outside the canvas.
- Positive/supported/aligned outcomes use green tones. Negative/unsupported/
  Review needed outcomes use red tones. Unknown or caution states use amber,
  and genuinely neutral states use gray.
- Segment colors are stable for a status during the current page session.
- Segment and legend selection use the same persistent selected treatment.
- Clicking a chart legend item applies or clears the same report filter as
  clicking its segment; report chart legends do not toggle data visibility.
- Charts provide a keyboard-accessible legend or equivalent list because canvas
  segments alone are not keyboard operable.
- Empty charts show **No VMs in scope** rather than an empty canvas.
- A zero-total chart shows no percentage and does not render synthetic slices.

## Report filtering model

Report interactions filter the VM table without modifying the existing filter
controls. Report filter state is visible in the selected report elements and is
cleared through Clear report filters. The shared report model owns the
reference population, focus population, visual populations, and facet-selection
mechanics; this section defines the Coverage Report's facets and feature effects.

Within one filter facet, multiple selected values are ORed. Different facets
are ANDed. Facets are:

- Dimension value
- Coverage category: Required - High, Required - Medium, Required - Low,
  defensive Required - Undefined when present, Unmarked, or Not required
- Coverage-decision status: Decision recorded or No decision
- Association: Assigned or Not associated
- Reservation status
- Prerequisite status
- Prerequisite with a Failed or Unknown result

Example: two selected subscriptions, Required - High or Not required, Assigned,
and Unsupported means:

`(Subscription A OR Subscription B) AND (Required - High OR Not required) AND Assigned AND Unsupported`.

Report filters operate on the full VM result after existing page filters and
search, not only the current VM-table page. Applying a report filter returns the
VM table to page 1. Sorting is retained. Any report-filter change clears all
checked VM rows, including applying, replacing, adding, removing, or resetting a
report selection.

### Shared dimension selection

Dimension values form one report-wide selection facet shared by Marking
Coverage, ODCR Association, and ODCR Prerequisites.

- Selecting a dimension row in any one of these tiles selects the matching row
  in all three tiles.
- A plain click on an unselected dimension value replaces the shared dimension
  selection and makes that tile's Dimension facet the active editing context.
  Ctrl+click on Windows/Linux or Command+click on macOS adds or removes values.
- Selected dimension values are ORed. They are ANDed with selections from other
  facets.
- A dimension value absent from a tile's current rows cannot be selected there,
  but an existing shared selection remains active and is exposed through the
  tile's selected-state summary.
- Plain-clicking a selected dimension value restarts report filtering: every
  report facet is cleared, that dimension value becomes the sole selection,
  and the clicked tile becomes the new active editing context.
- Ctrl+clicking or Command+clicking a selected dimension value removes only
  that value and preserves all other facet selections and the existing active
  editing context. Removing the last selected dimension clears only the
  Dimension facet.
- Changing Group by clears the shared dimension selection because values cannot
  be translated reliably between dimensions.

### Cross-visual interactions

The interaction model uses progressive cross-filtering with one active editing
context. The tile and facet changed by the latest filtering interaction preserve
their selectable alternatives; every other tile applies the complete filter
sequence.

- The VM table is **cross-filtered**. It displays only the focus population that
  matches every active report facet.
- Only the active editing tile excludes the facet changed by its latest
  interaction. An ODCR Association chart interaction excludes the Association
  facet in that tile.
- Every other tile applies all active facets. Selecting a facet in another tile
  moves the active editing context there, so the prior tile narrows to its
  selected rows and the newly active tile preserves its current alternatives.
- Counts, percentages, totals, sort values, and chart geometry use each tile's
  resulting visual population as their denominator.
- Fixed semantic categories remain available with zero values when their visual
  population has no matching records. This applies to Association and
  Prerequisite statuses.
- Dynamic grouped dimension rows, reservation statuses, and prerequisite issues
  may disappear when filters owned by other tiles leave them with no records.
- The active editing tile keeps alternatives for the edited facet visible while
  still applying every earlier facet. The VM table always applies all facets.
- Visual populations are always projected from the stable reference population;
  a tile never filters an already aggregated result. This prevents circular
  recomputation and visual feedback loops.
- Clearing report filters restores every visual population and the VM table
  to the reference population and clears the active editing context.

## VM table changes

### Separate prerequisite status

Add a **Prerequisite status** column next to Reservation status.

- It displays the backend `odcrSupportabilityStatus` value: Supported,
  Unsupported, or Unknown.
- Its badge styling reflects prerequisite status only.
- Reservation status badge styling reflects reservation status only and no
  longer changes because of prerequisite supportability.
- Clicking an actionable prerequisite-status badge opens the existing ODCR
  prerequisites drawer.
- The column is sortable and participates in CSV export.
- The existing Reservation status column remains sortable and continues to show
  the exact backend reservation status.

### Coverage decision tooltip

Hovering or keyboard-focusing the coverage decision badge/bullet displays the
shared tooltip style with:

1. The decision note at the top, when present.
2. Last updated date and time.
3. Last updated by display name.

When no note is present, the tooltip starts with audit metadata. When the VM has
never been marked and has no audit metadata, it says **No coverage decision has
been recorded**. Cleared decisions retain and display their latest audit
metadata, consistent with the existing backend contract.

The tooltip does not replace the edit button, and interacting with it must not
toggle VM row selection.

The tooltip is rendered in a viewport-level layer rather than inside the VM
table scroll container. It must remain fully visible when its badge is near a
table edge and must reposition above the badge when there is insufficient room
below it.

## Loading, empty, and error states

- The report grid uses stable tile dimensions while data is loading to avoid
  moving the VM table vertically.
- The desktop report has a fixed two-row height. Long grouped Coverage results
  scroll inside the Coverage tile instead of increasing the page height.
- Report and VM-table placeholders use the same skeleton color, shimmer speed,
  direction, and reduced-motion behavior. See `frontend-loading-states.md`.
- Each tile can show its own loading skeleton, empty state, or error state.
- Report-level **No VMs in scope** means existing page filters and search produce
  no reference rows. A tile-specific empty state means that tile's visual
  population has no rows after applying facets owned by other tiles.
- A tile-specific computation failure does not remove the other tiles.
- Backend/API error behavior remains the current page behavior because API
  changes are out of scope.
- A dimension with a missing value is a valid row named **Not mapped**, not an
  error or empty state.

## Accessibility

- All sortable headers, row selectors, column selectors, cells, chart legends,
  reset actions, and tooltips are keyboard reachable.
- Selected report controls expose `aria-pressed` or the appropriate grid
  selection state.
- Additive selection uses Ctrl+click and Ctrl+Space on Windows/Linux, and
  Command+click and Command+Space on macOS.
- Table headers use correct row and column scopes.
- Sticky regions do not obscure focused content.
- Color is not the sole indicator of percentage quality, selection, warning, or
  prerequisite status.
- Tooltips open on hover and focus and can be dismissed with Escape.
- Chart content has a textual summary available to assistive technology.

## Backend API

Not reworked in this phase. To be specified later.

The frontend implementation should consume the existing ODCR Coverage response
fields listed in Terminology. Any missing data needed to satisfy this document
must be raised before implementation rather than inferred or added to the API
without an explicit contract update.

## Existing page filters

Not reworked in this phase. To be specified later.

The report continues to use the population produced by the existing global
scope, VM filters, and text search. The only new filter behavior in this phase
is report-generated filtering described above.

## Open issues and UX decisions

The interaction and state-lifecycle contracts above are resolved. The following
details still require confirmation:

1. **Moving selected Unmarked.** This specification preserves Unmarked's active
  selection when moving it. Confirm whether moving it should instead clear the
  Coverage category facet to avoid a report population changing blocks while
  remaining selected.
2. **Empty block during active filtering.** Selecting only values owned by one
  block makes the other block empty. Confirm the empty-state copy; recommended
  text is **No matching coverage categories**, while all bullets remain visible
  in the active editing context.
3. **Count self-exclusion.** Counts apply every current filter except Coverage
  category itself. This keeps deselected alternatives visible and recoverable.
  Confirm this interpretation of “current filters”; applying the category
  facet itself would reduce deselected category counts to zero.

## Acceptance criteria

- The desktop report matches the specified two-row, three-column grid and the
  mobile reading order, with the ODCR Required, ODCR not Required, and unframed
  Coverage decision status blocks occupying the specified coordinates.
- One Group by control drives all dimension-aware tiles and includes None.
- ODCR Required shows coverage bullets for Required - High, Required - Medium,
  Required - Low, and its owned Unmarked category. ODCR not Required shows a
  selectable, non-movable Not required bullet and its owned Unmarked category.
- By default, the Coverage category facet is empty, every category is available,
  and no bullet is highlighted. The VM table is not category-filtered.
- Plain click selects or replaces one category; clicking the sole selected value
  clears the facet. Ctrl/Command interaction adds or removes values, which are
  ORed across both blocks to filter the VM table.
- Selected category bullets use the same persistent highlight and `aria-pressed`
  treatment as other report facets; no checkbox is displayed.
- Each block applies the active selection only to values it owns. With no active
  category selection, each block displays all of its owned categories.
- Category counts apply the current page and report filters while excluding the
  Coverage category facet itself.
- **Move Unmarked here** appears inline in the non-owner block, atomically moves
  Unmarked ownership while preserving selection, and moves itself to the other
  block.
- Unmarked ownership survives Group by changes, local and report filter resets,
  and data refresh in the current page, but a full browser reload restores its
  default ownership to ODCR Required.
- A malformed Required priority is included in ODCR Required as Required -
  Undefined, whose facet remains hidden when no such records exist.
- ODCR Association None mode combines a left-aligned Assigned/Not associated
  pie and right-side vertical legend with a Review placeholder.
- ODCR Unassociation shows the complementary Not associated percentage in
  grouped and None modes and never displays Review.
- Every report-generated filter is visible, affects the full VM-table result,
  and can be cleared without changing existing page filters.
- Dimension selections are shared and visibly synchronized across Marking
  Coverage, ODCR Association, ODCR Unassociation, and ODCR Prerequisites.
- Report selections progressively cross-filter the VM table and every report
  tile. Only the latest interaction's originating tile excludes its edited
  facet; every other tile applies all active facets.
- Fixed Association and Prerequisite statuses remain present at zero; dynamic
  grouped rows, reservation statuses, and issues may disappear when their
  visual population has no matching records.
- **Clear report filters** and **Reset VM filters** are separate, explicitly
  scoped actions in the report controls and VM filter controls respectively.
- Applying, replacing, adding, removing, or resetting any report filter clears
  all checked VM rows.
- ODCR Association grouped mode shows Dimension, Review, and Assigned,
  supports sorting, and uses dimension rows as report filters.
- Dimension-aware tile titles and first-column headers use the selected Group
  by display label in table mode and return to their base titles in None mode.
- Association depends only on reservation status: every status other than Not
  associated is Assigned. Review remains unavailable and displays an em dash.
- Coverage decision status displays percentage and muted `recorded / total`
  counts in its single measure column, with a compact overall pie beside the
  grouped table on wide viewports.
- Coverage decision status uses the same surrounding-content color for its tile and
  sticky table header. Its percentage and `recorded / total` values use the
  same table font size as ODCR Unassociation.
- All percentage totals are recomputed from absolute counts rather than averaged.
- Percentage display uses `0%` only for absolute zero, `<1%` for nonzero values
  below 1%, and whole percentages otherwise; sorting uses unrounded values.
- ODCR Prerequisites grouped mode shows a Supported versus Unsupported or
  unknown compact pie with a vertical clickable legend beside a two-column
  dimension/count table sorted by count descending.
- ODCR Prerequisites None mode uses the same Supported versus Unsupported or
  unknown pie and compound filter behavior as grouped mode.
- ODCR Prerequisite Issues remains a sortable prerequisite/count table for every
  Group by value, including None; its count combines Failed and Unknown results
  for each prerequisite, zero-count rows are omitted, and its rows filter the VM
  table.
- Prerequisite issue counts are distinct VM counts per prerequisite and never
  display a percentage or misleading grand total.
- Reservation status and prerequisite status appear in separate VM-table
  columns and no longer share badge semantics.
- Coverage decision note and audit metadata are available from the table by
  pointer and keyboard focus.
- All report pie charts use Apache ECharts rather than the custom conic-gradient
  implementation.
- ODCR Coverage Details has no center percentage and displays percentage labels
  inside pie segments large enough to label without overlap.
- ODCR Coverage Details displays its compact pie beside a semantic
  reservation-status table whose row markers match the pie colors; the compact
  Details and Coverage decision status pies are hidden at 1180px and below.
- Coverage Details defaults to VMs descending. Compact grouped charts align to
  the top of their tables; Coverage decision status uses a smaller center value
  and a vertical clickable legend below its pie.
- The three compact chart-and-table tiles use 108px pies. Every two-column count
  header is right-aligned with its values, and Coverage Details displays the
  complete **VMs** header.
- In None mode, chart-and-legend rows use a compact height, place the pie on the
  left, and vertically center the legend beside it. The Association Review
  placeholder is bottom-aligned independently and does not offset the legend.
- Report table scroll regions are constrained to the available tile height and
  inset from the right edge so scrollbar controls remain fully visible.
- Sortable report headers truncate within their column, preserve the sort
  indicator, and expose their complete label through a native title.
- Keyboard, focus, tooltip, empty-state, and narrow-viewport behavior meet the
  requirements in this document.