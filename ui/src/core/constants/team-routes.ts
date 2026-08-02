export function getTeamRoute(slug: string): string {
  return `/teams/${encodeURIComponent(slug)}`;
}
