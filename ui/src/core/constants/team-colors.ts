/**
 * Stable accent colours for teams.
 *
 * A team's colour is derived from its slug, never from list position or a
 * random draw, so the same team reads the same everywhere in the app. Slugs
 * are immutable server-side, which makes the mapping stable across renames.
 */

/**
 * Mantine palette keys used as team accents. Each has a light/dark aware
 * `-light`, `-light-color` and `-filled` CSS variable, so cards work in both
 * colour schemes without per-scheme overrides.
 */
export const TEAM_ACCENT_COLORS = [
  'indigo',
  'teal',
  'orange',
  'pink',
  'lime',
  'grape',
  'cyan',
  'yellow',
] as const;

export type TeamAccentColor = (typeof TEAM_ACCENT_COLORS)[number];

export type TeamAccent = {
  /** Mantine palette key, e.g. 'indigo'. */
  color: TeamAccentColor;
  /** Tinted surface that adapts to the colour scheme. */
  surface: string;
  /** Readable foreground on top of `surface`. */
  foreground: string;
  /** Saturated fill for solid accents such as the card spine. */
  solid: string;
};

/** FNV-1a over the slug's code units. Small, stable, and dependency free. */
function hashSlug(slug: string): number {
  let hash = 0x811c9dc5;
  for (let i = 0; i < slug.length; i++) {
    hash ^= slug.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193);
  }
  return hash >>> 0;
}

export function getTeamAccent(slug: string): TeamAccent {
  const color = TEAM_ACCENT_COLORS[hashSlug(slug) % TEAM_ACCENT_COLORS.length];

  return {
    color,
    surface: `var(--mantine-color-${color}-light)`,
    foreground: `var(--mantine-color-${color}-light-color)`,
    solid: `var(--mantine-color-${color}-filled)`,
  };
}

/** Up to two initials for a team, used as the card's map marker. */
export function getTeamInitials(displayName: string): string {
  const words = displayName
    .split(/[\s&/_-]+/)
    .map((word) => word.trim())
    .filter(Boolean);

  if (words.length === 0) return '?';
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return `${words[0][0]}${words[1][0]}`.toUpperCase();
}
