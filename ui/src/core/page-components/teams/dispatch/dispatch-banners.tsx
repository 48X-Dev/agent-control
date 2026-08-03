import { Alert, Stack, Text } from '@mantine/core';
import { IconHandStop, IconPlayerPause } from '@tabler/icons-react';

import type { DispatchStateSnapshot } from '@/core/api/types';

import { formatDateTime } from './formatting';

/**
 * What a stop does not do, said where the stop is visible.
 *
 * Neither switch kills a tool that is already executing, and killing the
 * process would not unwind the email it already sent. An operator reading a
 * banner that implies otherwise will stop watching too early.
 */
const RUNNING_TOOL_CAVEAT =
  'A tool that is already executing is not stopped by this, and nothing in ' +
  'this console unwinds an action a tool has already taken.';

function setterLine(
  at: string | null | undefined,
  byHash: string | null | undefined
): string | null {
  const when = formatDateTime(at);
  if (!when && !byHash) return null;

  // A credential, not a person. Every browser caller hashes identically
  // today, so rendering this as a name would be an invention.
  const who = byHash ? ` by the credential ${byHash}` : '';
  return when ? `Set ${when}${who}.` : `Set${who}.`;
}

export function DispatchBanners({
  state,
}: {
  state: DispatchStateSnapshot | null | undefined;
}) {
  if (!state) return null;
  if (!state.paused && !state.executors_halted) return null;

  return (
    <Stack gap="xs" data-testid="dispatch-banners">
      {state.paused ? (
        <Alert
          variant="light"
          color="yellow"
          icon={<IconPlayerPause size={16} />}
          title="New agent work is paused in this namespace"
          data-testid="dispatch-paused-banner"
        >
          <Stack gap={4}>
            <Text size="xs">
              Nothing new is imported, claimed or started while this is on.
              Turns already under way keep running. {RUNNING_TOOL_CAVEAT}
            </Text>
            {state.paused_reason ? (
              <Text size="xs" data-testid="dispatch-paused-reason">
                Reason given: {state.paused_reason}
              </Text>
            ) : null}
            {setterLine(state.paused_at, state.paused_by_hash) ? (
              <Text size="xs" c="dimmed">
                {setterLine(state.paused_at, state.paused_by_hash)}
              </Text>
            ) : null}
            <Text size="xs" c="dimmed">
              Clearing it is an admin operation and is not offered here.
            </Text>
          </Stack>
        </Alert>
      ) : null}

      {state.executors_halted ? (
        <Alert
          variant="light"
          color="red"
          icon={<IconHandStop size={16} />}
          title="Executors are halted in this namespace"
          data-testid="dispatch-halted-banner"
        >
          <Stack gap={4}>
            <Text size="xs">
              Every new turn and every new session is refused, human chat
              sessions included. {RUNNING_TOOL_CAVEAT}
            </Text>
            {state.executors_halted_reason ? (
              <Text size="xs" data-testid="dispatch-halted-reason">
                Reason given: {state.executors_halted_reason}
              </Text>
            ) : null}
            {setterLine(
              state.executors_halted_at,
              state.executors_halted_by_hash
            ) ? (
              <Text size="xs" c="dimmed">
                {setterLine(
                  state.executors_halted_at,
                  state.executors_halted_by_hash
                )}
              </Text>
            ) : null}
          </Stack>
        </Alert>
      ) : null}
    </Stack>
  );
}
