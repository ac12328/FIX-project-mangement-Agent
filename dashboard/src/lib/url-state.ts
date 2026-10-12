export function updateSearchParams(
  current: URLSearchParams | ReadonlyURLSearchParams,
  updates: Record<string, string | number | boolean | null | undefined>,
): string {
  const next = new URLSearchParams(current.toString());
  for (const [key, value] of Object.entries(updates)) {
    if (value === null || value === undefined || value === "") next.delete(key);
    else next.set(key, String(value));
  }
  return next.toString();
}

type ReadonlyURLSearchParams = Pick<URLSearchParams, "toString">;

export function pageFromSearchParam(value: string | null): number {
  const page = Number.parseInt(value ?? "1", 10);
  return Number.isFinite(page) && page > 0 ? page : 1;
}
