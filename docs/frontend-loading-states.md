# Frontend loading-state convention

All skeleton placeholders in the SPA use one visual language so a page does
not appear to contain unrelated loading systems.

## Skeleton style

- Use the shared left-to-right shimmer gradient defined by
  `skeleton-shimmer` in `web/styles/vm-coverage.css`.
- Use the same 1.4-second linear timing for report tiles, tables, checkboxes,
  and future skeleton shapes.
- Vary only the placeholder dimensions needed to approximate the final content.
  Do not introduce a feature-specific color, pulse, direction, or duration.
- Under `prefers-reduced-motion: reduce`, disable animation and use the shared
  static opacity treatment.

Spinners remain appropriate for compact command progress and status indicators.
Skeletons are used when reserving the geometry of content that is being loaded.

## Layout stability

Loading and loaded states reserve the same outer dimensions. Content with an
unbounded number of rows scrolls within its card or table viewport; it must not
increase the page height after loading.

## Review checklist

- Loading and loaded containers have matching dimensions.
- Every skeleton shape uses the shared shimmer declaration.
- Reduced-motion behavior is preserved.
- The loading state does not create horizontal or vertical page overflow.