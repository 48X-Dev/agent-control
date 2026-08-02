import { Group, Select, Text } from '@mantine/core';
import { Button } from '@rungalileo/jupiter-ds';
import { IconPlus } from '@tabler/icons-react';
import { useMemo } from 'react';

import type { AgentSessionSummary } from '@/core/api/types';
import { sessionLabel } from '@/core/hooks/query-hooks/use-agent-sessions';

type SessionSwitcherProps = {
  sessions: AgentSessionSummary[];
  activeSessionKey: string | null;
  onSelect: (sessionKey: string) => void;
  onNewChat: () => void;
  isLoading: boolean;
  isCreating: boolean;
  /**
   * Whether "New chat" is offered at all. Whether it will *succeed* is the
   * server's answer, not this panel's guess: an agent with no executor
   * binding, or an executor that is switched off, is a written refusal shown
   * inline rather than a disabled button with nothing to explain it.
   */
  canCreate: boolean;
};

/**
 * Which conversation the panel is showing, and how to start another.
 *
 * A session's own status is part of its label rather than a separate badge:
 * an archived or orphaned chat still opens, and someone picking from a list
 * should see which one they are picking before they pick it.
 */
export function SessionSwitcher({
  sessions,
  activeSessionKey,
  onSelect,
  onNewChat,
  isLoading,
  isCreating,
  canCreate,
}: SessionSwitcherProps) {
  const options = useMemo(
    () =>
      sessions.map((session) => ({
        value: session.session_key,
        label:
          session.status === 'active'
            ? sessionLabel(session)
            : `${sessionLabel(session)} · ${session.status.replace(/_/g, ' ')}`,
      })),
    [sessions]
  );

  return (
    <Group justify="space-between" align="center" wrap="nowrap" gap="sm">
      {sessions.length > 0 ? (
        <Select
          value={activeSessionKey}
          onChange={(value) => {
            if (value) onSelect(value);
          }}
          data={options}
          allowDeselect={false}
          searchable={sessions.length > 8}
          disabled={isLoading}
          size="sm"
          w={320}
          maxDropdownHeight={280}
          aria-label="Chat session"
          data-testid="chat-session-switcher"
        />
      ) : (
        <Text size="sm" c="dimmed" data-testid="chat-session-switcher-empty">
          No chats with this agent yet.
        </Text>
      )}

      <Button
        variant="outline"
        size="sm"
        leftSection={<IconPlus size={14} />}
        onClick={onNewChat}
        loading={isCreating}
        disabled={!canCreate || isCreating}
        data-testid="chat-new-session"
      >
        New chat
      </Button>
    </Group>
  );
}
