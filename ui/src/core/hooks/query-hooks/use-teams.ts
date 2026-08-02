import { useQueries, useQuery } from '@tanstack/react-query';
import { useMemo } from 'react';

import { api } from '@/core/api/client';
import { isNotFoundError } from '@/core/api/errors';
import type {
  GetTeamResponse,
  ListTeamsResponse,
  TeamSummary,
} from '@/core/api/types';

const TEAMS_PAGE_SIZE = 100;

export const teamsQueryKey = ['teams', 'list'] as const;

export function teamQueryKey(slug: string) {
  return ['team', slug] as const;
}

/**
 * Query hook to fetch the teams in the current namespace.
 *
 * Teams are a small, slow-moving set, so a single page covers every realistic
 * namespace and the overview can render without pagination.
 */
export function useTeams() {
  return useQuery<ListTeamsResponse>({
    queryKey: teamsQueryKey,
    queryFn: async () => {
      const { data, error } = await api.teams.list({ limit: TEAMS_PAGE_SIZE });
      if (error) throw error;
      return data!;
    },
  });
}

/**
 * One team and its members.
 *
 * Shares a cache key with the per-team reads the overview issues, so arriving
 * on a detail page from the overview usually renders without a second fetch.
 * An unknown slug is not retried: a 404 will not become a 200 on the way back.
 */
export function useTeam(slug: string) {
  return useQuery<GetTeamResponse>({
    queryKey: teamQueryKey(slug),
    queryFn: async () => {
      const { data, error } = await api.teams.get(slug);
      if (error) throw error;
      return data!;
    },
    enabled: Boolean(slug),
    retry: (failureCount, error) => !isNotFoundError(error) && failureCount < 1,
  });
}

export type TeamWithMembers = TeamSummary & {
  /** Member agent names, or undefined while the team's members load. */
  memberNames?: string[];
  isLoadingMembers: boolean;
};

type MemberSlice = {
  names?: string[];
  isPending: boolean;
};

/**
 * Teams plus the agent names in each one.
 *
 * The list endpoint carries member counts but not the members themselves, so
 * names come from the per-team endpoint. Those queries are cached under the
 * same key a team detail view would use, and teams already known to be empty
 * are never requested. Cards render from the list result immediately; names
 * fill in as each team resolves.
 */
export function useTeamsWithMembers() {
  const teamsQuery = useTeams();

  const teams = useMemo(
    () => teamsQuery.data?.teams ?? [],
    [teamsQuery.data?.teams]
  );

  const memberSlices = useQueries({
    queries: teams.map((team) => ({
      queryKey: teamQueryKey(team.slug),
      queryFn: async () => {
        const { data, error } = await api.teams.get(team.slug);
        if (error) throw error;
        return data!;
      },
      enabled: team.member_count > 0,
    })),
    // Collapsing to the fields the cards use keeps this array referentially
    // stable between renders, so the composition below memoizes properly.
    combine: (results): MemberSlice[] =>
      results.map((result) => ({
        names: result.data?.members.map((member) => member.agent_name),
        isPending: result.isPending,
      })),
  });

  const teamsWithMembers: TeamWithMembers[] = useMemo(
    () =>
      teams.map((team, index) => {
        if (team.member_count === 0) {
          return { ...team, memberNames: [], isLoadingMembers: false };
        }

        const slice = memberSlices[index];
        return {
          ...team,
          memberNames: slice?.names,
          isLoadingMembers: slice?.isPending ?? true,
        };
      }),
    [teams, memberSlices]
  );

  return {
    teams: teamsWithMembers,
    isLoading: teamsQuery.isLoading,
    error: teamsQuery.error,
  };
}
