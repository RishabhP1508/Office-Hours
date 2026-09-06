// Small date/URL formatting helpers shared by Header, SourceList, and Freshness -- none of these
// talk to the backend (see lib/api.ts, the only module that does).

export function hostnameOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

// Every date this app displays (rule_effective_date, page_last_updated, last_verified_at,
// fetched_at, generated_at) is computed and stored by the backend in UTC. Formatting with the
// browser's local timeZone instead of "UTC" would read a date-only string like "2026-09-15" as UTC
// midnight, then print whatever calendar day that instant falls on in the visitor's own timezone --
// "September 14, 2026" for anyone west of UTC. For a product whose whole job is stating the right
// date, that one-day drift is exactly the kind of error it cannot afford, so every formatter here
// pins `timeZone: "UTC"` rather than trusting the browser's local zone.
export function formatLongDate(isoDate: string): string {
  return new Intl.DateTimeFormat("en-US", {
    month: "long",
    day: "numeric",
    year: "numeric",
    timeZone: "UTC",
  }).format(new Date(isoDate));
}

export function formatShortDate(isoDate: string): string {
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
    timeZone: "UTC",
  }).format(new Date(isoDate));
}

// "22 Aug 2026" -- the header's stale-state date, deliberately terser than formatShortDate's
// "Aug 22, 2026" (no comma) since this sits inline in a single short trust-indicator line.
export function formatDayMonthYear(isoDateTime: string): string {
  return new Intl.DateTimeFormat("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  }).format(new Date(isoDateTime));
}

/** True if the datetime `isoDateTime` falls on the same calendar day as the date `isoDate`
 * (both read as UTC calendar dates, matching how the backend stamps `as_of`/`last_verified_at`). */
export function isSameCalendarDay(isoDateTime: string, isoDate: string): boolean {
  return isoDateTime.slice(0, 10) === isoDate;
}
