import { Group, Stack, Text, TextInput } from '@mantine/core';
import { Button } from '@rungalileo/jupiter-ds';
import { type FormEvent, useState } from 'react';

import { getErrorStatus } from '@/core/api/errors';
import { useLinkLinearTeam } from '@/core/hooks/query-hooks/use-link-linear-team';

/** Linear team keys are short, unpunctuated identifiers such as ENG or SALES. */
const LINEAR_TEAM_KEY_PATTERN = /^[A-Za-z0-9]+$/;
const LINEAR_TEAM_KEY_MAX_LENGTH = 20;

type LinkLinearTeamProps = {
  slug: string;
  /** Prefilled when the team is already linked and the key is being changed. */
  currentKey?: string | null;
  onLinked?: () => void;
  onCancel?: () => void;
};

function failureMessage(error: unknown): string {
  const status = getErrorStatus(error);
  if (status === 403) {
    return 'Linking a Linear team needs an admin API key. Sign in with one and try again.';
  }
  if (status === 422 || status === 400) {
    return 'Linear rejected that key. Use the short prefix from your Linear issue IDs, such as ENG.';
  }
  return error instanceof Error
    ? error.message
    : 'Could not save the Linear team key.';
}

/**
 * Form for pointing a team at a Linear team.
 *
 * The value here is a Linear team key, the prefix on that team's issue IDs.
 * It is not a credential: the Linear API key lives on the server and never
 * reaches the browser.
 */
export function LinkLinearTeam({
  slug,
  currentKey,
  onLinked,
  onCancel,
}: LinkLinearTeamProps) {
  const [value, setValue] = useState(currentKey ?? '');
  const [validationError, setValidationError] = useState<string | null>(null);
  const link = useLinkLinearTeam(slug);

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const trimmed = value.trim();

    if (!LINEAR_TEAM_KEY_PATTERN.test(trimmed)) {
      setValidationError('Use letters and digits only, with no spaces.');
      return;
    }

    setValidationError(null);
    link.mutate(trimmed.toUpperCase(), { onSuccess: () => onLinked?.() });
  };

  return (
    <form onSubmit={handleSubmit} data-testid="link-linear-team-form">
      <Stack gap="xs">
        <TextInput
          label="Linear team key"
          description="The prefix on that team's issue IDs, for example ENG."
          placeholder="ENG"
          value={value}
          maxLength={LINEAR_TEAM_KEY_MAX_LENGTH}
          onChange={(event) => {
            setValue(event.currentTarget.value);
            if (validationError) setValidationError(null);
          }}
          error={validationError}
          disabled={link.isPending}
          data-testid="linear-team-key-input"
        />

        {link.isError ? (
          <Text size="xs" c="red" data-testid="link-linear-team-error">
            {failureMessage(link.error)}
          </Text>
        ) : null}

        <Group gap="xs">
          <Button
            type="submit"
            variant="filled"
            size="sm"
            loading={link.isPending}
            data-testid="link-linear-team-submit"
          >
            {currentKey ? 'Update link' : 'Link team'}
          </Button>
          {onCancel ? (
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={onCancel}
              disabled={link.isPending}
              data-testid="link-linear-team-cancel"
            >
              Cancel
            </Button>
          ) : null}
        </Group>
      </Stack>
    </form>
  );
}
