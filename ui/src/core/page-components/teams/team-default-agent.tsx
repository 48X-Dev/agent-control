import { Badge, Box, Group, Select, Stack, Text, Tooltip } from '@mantine/core';
import { Button } from '@rungalileo/jupiter-ds';
import { type FormEvent, useState } from 'react';

import { getErrorStatus } from '@/core/api/errors';
import type { TeamMemberRef } from '@/core/api/types';
import { useHasAdminCredential } from '@/core/hooks/query-hooks/use-admin-credential';
import { useSetTeamDefaultAgent } from '@/core/hooks/query-hooks/use-set-team-default-agent';
import { useTeam } from '@/core/hooks/query-hooks/use-teams';

import classes from './team-detail.module.css';

/**
 * Stands for "clear the default". Agent names match `^[a-z0-9:_-]+$`, so
 * parentheses put this value outside the set a real name can occupy.
 */
const NO_DEFAULT = '(none)';

const EXPLANATION =
  'Runs a dispatched step that names no agent, under that agent’s ' +
  'controls. With no default, the dispatcher blocks those tasks rather than ' +
  'choosing an agent.';

function failureMessage(error: unknown): string {
  const status = getErrorStatus(error);
  if (status === 403) {
    return 'Setting the default agent needs an admin API key. Sign in with one and try again.';
  }
  if (status === 409) {
    return 'That agent is not in this team. Add it to the team first.';
  }
  if (status === 422 || status === 400) {
    return 'The server rejected that agent name.';
  }
  return error instanceof Error
    ? error.message
    : 'Could not save the default agent.';
}

type DefaultAgentFormProps = {
  slug: string;
  current: string | null;
  members: TeamMemberRef[];
  onSaved: () => void;
  onCancel: () => void;
};

/**
 * The picker itself.
 *
 * Options are this team's own members and nothing else. The server refuses a
 * non-member with 409 AGENT_NOT_IN_TEAM, so a wider list could only offer
 * choices that cannot be saved, and the field exists to say which agent's
 * controls govern this team's work - an agent nobody added to the team has no
 * business answering that.
 */
function DefaultAgentForm({
  slug,
  current,
  members,
  onSaved,
  onCancel,
}: DefaultAgentFormProps) {
  const [value, setValue] = useState(current ?? NO_DEFAULT);
  const save = useSetTeamDefaultAgent(slug);

  // A stored default that is not a member can exist: rows written before the
  // server enforced membership, or edited outside the API. Carrying it as its
  // own option keeps the picker showing the value it actually holds, instead
  // of a blank input on a team that visibly has a default set.
  const stale =
    current !== null && !members.some((m) => m.agent_name === current);

  const options = [
    { value: NO_DEFAULT, label: 'No default agent' },
    ...(stale
      ? [{ value: current, label: `${current} (not in this team)` }]
      : []),
    ...members.map((member) => ({
      value: member.agent_name,
      label: member.agent_name,
    })),
  ];

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    save.mutate(value === NO_DEFAULT ? null : value, { onSuccess: onSaved });
  };

  return (
    <form onSubmit={handleSubmit} data-testid="team-default-agent-form">
      <Stack gap="xs">
        <Select
          label="Default agent"
          description={EXPLANATION}
          data={options}
          value={value}
          onChange={(next) => setValue(next ?? NO_DEFAULT)}
          allowDeselect={false}
          searchable={members.length > 8}
          disabled={save.isPending}
          data-testid="team-default-agent-select"
          comboboxProps={{ withinPortal: false }}
        />

        {save.isError ? (
          <Text size="xs" c="red" data-testid="team-default-agent-error">
            {failureMessage(save.error)}
          </Text>
        ) : null}

        <Group gap="xs">
          <Button
            type="submit"
            variant="filled"
            size="sm"
            loading={save.isPending}
            data-testid="team-default-agent-submit"
          >
            Save
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={onCancel}
            disabled={save.isPending}
            data-testid="team-default-agent-cancel"
          >
            Cancel
          </Button>
        </Group>
      </Stack>
    </form>
  );
}

/** A Change button that explains why it will not do anything. */
function BlockedChange({ reason }: { reason: string }) {
  return (
    <Tooltip label={reason}>
      <Box data-testid="team-default-agent-blocked" data-reason={reason}>
        <Button
          variant="ghost"
          size="sm"
          disabled
          data-testid="team-default-agent-change"
        >
          Change
        </Button>
      </Box>
    </Tooltip>
  );
}

/**
 * The team's default agent, shown and edited on the team page.
 *
 * Team configuration rather than a choice at the moment of dispatch: controls
 * are bound per agent, so naming the agent names the control surface the work
 * runs under. Pressing play is authenticated, writing a team is admin, and
 * this decision belongs on the admin side of that line.
 *
 * Because the write is admin-gated, a read-only session is shown a disabled
 * control with the reason rather than one that can only 403. That verdict
 * comes from an admin-tier probe, not from the login response, which says
 * nothing about a session resumed from its cookie - the ordinary case here.
 *
 * The agent name is rendered as a text node. It is operator-supplied text on a
 * page whose session cookie is a credential on every admin endpoint, so it is
 * never a link target, never markup, and never interpolated into a URL here.
 */
export function TeamDefaultAgent({ slug }: { slug: string }) {
  const { data: team } = useTeam(slug);
  const isAdmin = useHasAdminCredential();
  const [editing, setEditing] = useState(false);

  if (!team) return null;

  const current = team.default_agent_name ?? null;
  const members = team.members;

  // A team with no members has nothing to pick - unless it still holds a
  // default from before, in which case clearing it must stay reachable.
  const nothingToPick = members.length === 0 && current === null;

  // `isAdmin` is undefined while the probe is in flight. Only a proven refusal
  // blocks, so an admin never waits to be told they may act.
  const blockedReason =
    isAdmin === false
      ? 'Requires an admin key'
      : nothingToPick
        ? 'Add an agent to this team first'
        : null;

  if (editing) {
    return (
      <Box className={classes.hint}>
        <DefaultAgentForm
          slug={slug}
          current={current}
          members={members}
          onSaved={() => setEditing(false)}
          onCancel={() => setEditing(false)}
        />
      </Box>
    );
  }

  return (
    <Box className={classes.hint} data-testid="team-default-agent">
      <Group justify="space-between" align="flex-start" wrap="nowrap" gap="sm">
        <Stack gap={4} style={{ minWidth: 0 }}>
          <Group gap={8} align="center" wrap="nowrap">
            <Text size="sm" fw={500}>
              Default agent
            </Text>
            {current ? (
              <Badge
                size="sm"
                variant="light"
                color="gray"
                // Agent names are lower-case identifiers and the rows below
                // print them verbatim. The badge default would upper-case
                // this one and stop it matching the agent it names.
                tt="none"
                data-testid="team-default-agent-value"
              >
                {current}
              </Badge>
            ) : (
              <Text size="sm" c="dimmed" data-testid="team-default-agent-unset">
                Not set
              </Text>
            )}
          </Group>
          <Text size="xs" c="dimmed">
            {EXPLANATION}
          </Text>
        </Stack>

        {blockedReason ? (
          <BlockedChange reason={blockedReason} />
        ) : (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setEditing(true)}
            data-testid="team-default-agent-change"
          >
            Change
          </Button>
        )}
      </Group>
    </Box>
  );
}
