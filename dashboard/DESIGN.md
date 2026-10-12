# AgentFIXBPRO Dashboard Design System

## Direction

AgentFIXBPRO is a neutral, high-density enterprise operations workspace. Neutral surfaces carry the interface; teal identifies the product, current navigation, and primary actions. Red, amber, and green are reserved for real operational status.

## Typography

- **Interface and body:** Plus Jakarta Sans
- **Headings:** Bricolage Grotesque
- **Identifiers, metrics, and timestamps:** JetBrains Mono
- Body text defaults to 16px with 1.5 line height.
- Metadata is never smaller than 12px.
- Headings use restrained weight and sentence case.

## Spacing and shape

- Use a 4/8px rhythm: 4, 8, 12, 16, 24, 32, 40, 48.
- Page gutters: 16px mobile, 24px tablet, 32px desktop.
- Touch targets are at least 44 by 44px.
- Cards and panels use 12–16px radii; controls use 8–12px radii.
- Prefer borders and surface contrast over decorative shadows.

## Token architecture

### Primitive

Neutral and teal scales are declared in `src/app/globals.css`.

### Semantic

- `background`, `foreground`: workspace canvas and primary text
- `card`, `popover`: elevated working surfaces
- `muted`: secondary surfaces and supporting text
- `primary`: product and primary action
- `success`, `warning`, `danger`, `info`: operational state only
- `border`, `input`, `ring`: structure and focus

### Component

Components consume semantic Tailwind utilities such as `bg-card`, `text-muted-foreground`, `border-border`, and `focus-visible:ring-ring`. Do not add raw color values inside route components.

## Core patterns

- `PageHeader`: title, concise context, refresh metadata, and primary actions
- `PageToolbar`: URL-backed search, filters, sorting, and record summary
- `MetricCard`: one operational metric with a clear label and supporting detail
- `SectionPanel`: bordered surface with a consistent header and local action
- `DataTable`: dense desktop table with structured mobile rows
- `StatePanel`: explicit loading, empty, and error states with recovery action
- `StatusBadge`: semantic state label; color is never the only signal
- `Pagination`: server-backed page controls and visible range

## Navigation

- Desktop sidebar supports expanded and icon-rail modes.
- Mobile navigation is a modal drawer with focus trapping, Escape dismissal, and background scroll lock.
- Primary groups are Home, Work, Projects, and AI & Analytics.
- Learning and Admin contain nested destinations.
- Account destinations live in the user menu.
- Permission-restricted destinations are omitted instead of shown disabled.

## Interaction and motion

- Motion must communicate state or spatial change.
- Use 150–250ms transitions; avoid page-load stagger, card lift, decorative grain, and atmospheric effects.
- Loading indicators may spin or pulse when they communicate active work.
- Respect `prefers-reduced-motion` globally.
- Every mutation must expose pending, success, and failure feedback; destructive or reversible changes should offer undo when the domain API supports it.

## Responsive and accessibility

- No page-level horizontal scrolling; tables may scroll inside their own container.
- Desktop tables become structured rows on narrow screens.
- Focus is always visible and offset from sticky navigation.
- Keyboard-only operation, 200% zoom, light/dark themes, and reduced motion are release requirements.
- Normal text targets WCAG AA contrast; status meaning is repeated in text or iconography.
