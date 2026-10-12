/** Return the canonical project identifier used by dashboard API routes. */
export function canonicalizeProjectRouteId(value: string | null | undefined): string {
  return (value ?? "").trim().toUpperCase();
}
