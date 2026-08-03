import {
  Alert,
  Badge,
  Box,
  Center,
  Group,
  Loader,
  Modal,
  ScrollArea,
  Stack,
  Text,
  Textarea,
  Tooltip,
} from '@mantine/core';
import { Button } from '@rungalileo/jupiter-ds';
import { IconAlertCircle, IconHistory } from '@tabler/icons-react';
import { useState } from 'react';

import { getErrorCode, isConflictError } from '@/core/api/errors';
import type {
  AgentConfigVersionSummary,
  ConfigEventType,
  GetAgentConfigResponse,
} from '@/core/api/types';
import {
  CONFIG_NOTE_MAX_LENGTH,
  CONFIG_PICKUP_COPY,
} from '@/core/hooks/query-hooks/use-agent-config';
import {
  useAgentConfigVersion,
  useAgentConfigVersions,
} from '@/core/hooks/query-hooks/use-agent-config-versions';
import { useRestoreAgentConfigVersion } from '@/core/hooks/query-hooks/use-restore-agent-config';

import { ConfigDiff } from './config-diff';
import classes from './config-tab.module.css';

const EVENT_LABEL: Record<ConfigEventType, string> = {
  created: 'created',
  updated: 'updated',
  prompt_cleared: 'prompt cleared',
  model_cleared: 'model cleared',
  restored: 'restored',
  enabled: 'delivery on',
  disabled: 'delivery off',
};

const EVENT_COLOR: Record<ConfigEventType, string> = {
  created: 'blue',
  updated: 'blue',
  prompt_cleared: 'gray',
  model_cleared: 'gray',
  restored: 'violet',
  enabled: 'teal',
  disabled: 'orange',
};

function formatWhen(iso: string): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return iso;
  return at.toLocaleString();
}

type ConfigHistoryProps = {
  agentName: string;
  config: GetAgentConfigResponse;
  canWrite: boolean;
  /**
   * Save an old body against the model currently configured. The explicit
   * alternative when a restore is refused because the version names a model
   * the server no longer offers.
   */
  onRestorePromptOnly: (body: string) => void;
  restorePromptOnlyPending: boolean;
};

/**
 * What this configuration has been, and how to put an old one back.
 *
 * Rollback creates a new version rather than rewinding the counter. A shared
 * history that can be rewritten is a history nobody can reason about, so
 * restoring version 3 onto version 9 produces version 10 and says so in the
 * confirm dialog.
 */
export function ConfigHistory({
  agentName,
  config,
  canWrite,
  onRestorePromptOnly,
  restorePromptOnlyPending,
}: ConfigHistoryProps) {
  const versionsQuery = useAgentConfigVersions(agentName);
  const restore = useRestoreAgentConfigVersion(agentName);

  const [openVersion, setOpenVersion] = useState<number | null>(null);
  const [mode, setMode] = useState<'view' | 'restore'>('view');
  const [note, setNote] = useState('');

  const detail = useAgentConfigVersion(agentName, openVersion);
  const versions = versionsQuery.data?.versions ?? [];

  const closeModal = () => {
    setOpenVersion(null);
    setNote('');
    restore.reset();
  };

  const open = (versionNum: number, next: 'view' | 'restore') => {
    setOpenVersion(versionNum);
    setMode(next);
    setNote('');
    restore.reset();
  };

  // Three different things answer 409 on a restore: this editor's version is
  // stale, the stored body format is one the server no longer understands, and
  // the version names a model that has left the allowlist. Only the last is
  // fixable by putting the prompt text back on its own, so the status alone
  // cannot decide what to offer. Telling somebody whose colleague just saved
  // that their model is the problem, and handing them a button that would fail
  // the same way, is worse than saying nothing.
  const restoreFailedOnModel =
    restore.isError && getErrorCode(restore.error) === 'MODEL_NOT_ALLOWED';
  const restoreFailedOnConflict =
    restore.isError && !restoreFailedOnModel && isConflictError(restore.error);

  const body = () => {
    if (versionsQuery.isPending) {
      return (
        <Center py="xl">
          <Loader size="sm" />
        </Center>
      );
    }

    if (versionsQuery.isError) {
      return (
        <Box className={classes.panelBody}>
          <Alert
            icon={<IconAlertCircle size={16} />}
            color="red"
            variant="light"
            title="History could not be loaded"
          >
            <Text size="sm">
              {versionsQuery.error instanceof Error
                ? versionsQuery.error.message
                : 'The version history did not load.'}
            </Text>
          </Alert>
        </Box>
      );
    }

    if (versions.length === 0) {
      return (
        <Box className={classes.panelBody}>
          <Text size="sm" c="dimmed">
            Nothing saved yet. The first save appears here, and every change
            after it, including clearing a field.
          </Text>
        </Box>
      );
    }

    return (
      <ScrollArea.Autosize className={classes.historyList} type="auto">
        {versions.map((version) => (
          <HistoryRow
            key={version.version_num}
            version={version}
            isCurrent={version.version_num === config.current_version}
            canWrite={canWrite}
            onView={() => open(version.version_num, 'view')}
            onRestore={() => open(version.version_num, 'restore')}
          />
        ))}
      </ScrollArea.Autosize>
    );
  };

  return (
    <Box className={classes.panel} data-testid="config-history">
      <Box className={classes.panelHeader}>
        <Group gap="xs">
          <IconHistory size={16} />
          <Text size="sm" fw={600}>
            History
          </Text>
        </Group>
        <Text size="xs" c="dimmed" mt={4}>
          Every change, oldest at the bottom. Restoring adds a version rather
          than removing one.
        </Text>
      </Box>

      {body()}

      <Modal
        opened={openVersion !== null}
        onClose={closeModal}
        size="xl"
        title={
          mode === 'restore'
            ? `Restore version ${openVersion ?? ''}`
            : `Version ${openVersion ?? ''}`
        }
        styles={{ title: { fontWeight: 600 } }}
      >
        {detail.isPending ? (
          <Center py="xl">
            <Loader size="sm" />
          </Center>
        ) : detail.isError || !detail.data ? (
          <Alert
            icon={<IconAlertCircle size={16} />}
            color="red"
            variant="light"
            title="That version could not be loaded"
          >
            <Text size="sm">
              {detail.error instanceof Error
                ? detail.error.message
                : 'The version did not load.'}
            </Text>
          </Alert>
        ) : (
          <Stack gap="md">
            <ConfigDiff
              before={{
                label: `Version ${detail.data.version.version_num}`,
                body: detail.data.version.body,
                modelId: detail.data.version.model_id,
              }}
              after={{
                label: 'the configuration in effect now',
                body: config.body,
                modelId: config.model_id,
              }}
            />

            {mode === 'restore' ? (
              <Stack gap="sm">
                <Alert color="blue" variant="light">
                  <Stack gap={4}>
                    <Text size="sm">
                      Restoring copies this version&apos;s prompt and model
                      forward as version {config.current_version + 1}. Version
                      numbers never rewind, so the change you are undoing stays
                      in the history.
                    </Text>
                    <Text size="xs" c="dimmed">
                      {CONFIG_PICKUP_COPY}. Prompt delivery is not switched back
                      on by a restore; that is a separate toggle.
                    </Text>
                  </Stack>
                </Alert>

                <Textarea
                  label="Note"
                  description="Optional. Recorded on the new version row."
                  placeholder="Why this is going back"
                  value={note}
                  maxLength={CONFIG_NOTE_MAX_LENGTH}
                  onChange={(event) => setNote(event.currentTarget.value)}
                  autosize
                  minRows={2}
                  data-testid="restore-note-input"
                />

                {restore.isError ? (
                  <Alert
                    icon={<IconAlertCircle size={16} />}
                    color={
                      restoreFailedOnModel || restoreFailedOnConflict
                        ? 'orange'
                        : 'red'
                    }
                    variant="light"
                    title={
                      restoreFailedOnModel
                        ? 'This version could not be restored whole'
                        : restoreFailedOnConflict
                          ? 'Somebody else saved while this was open'
                          : 'The restore did not happen'
                    }
                    data-testid="restore-error"
                  >
                    <Stack gap={4}>
                      <Text size="sm">
                        {restore.error instanceof Error
                          ? restore.error.message
                          : 'The restore was refused.'}
                      </Text>
                      {restoreFailedOnModel && detail.data.version.body ? (
                        <Text size="xs" c="dimmed">
                          Nothing was written. You can put the prompt text back
                          on its own and leave the model as it is now.
                        </Text>
                      ) : null}
                      {restoreFailedOnConflict ? (
                        <Text size="xs" c="dimmed">
                          Nothing was written. Reload the page to see what
                          changed, then restore again from the version you still
                          want.
                        </Text>
                      ) : null}
                    </Stack>
                  </Alert>
                ) : null}

                <Group justify="flex-end" gap="xs">
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={closeModal}
                    data-testid="restore-cancel"
                  >
                    Cancel
                  </Button>
                  {restoreFailedOnModel && detail.data.version.body ? (
                    <Button
                      variant="outline"
                      size="sm"
                      loading={restorePromptOnlyPending}
                      onClick={() => {
                        onRestorePromptOnly(detail.data!.version.body!);
                        closeModal();
                      }}
                      data-testid="restore-prompt-only"
                    >
                      Restore the prompt text, keep the current model
                    </Button>
                  ) : null}
                  <Button
                    variant="filled"
                    size="sm"
                    loading={restore.isPending}
                    onClick={() => {
                      restore.mutate(
                        {
                          versionNum: detail.data!.version.version_num,
                          expected_version: config.current_version,
                          note: note.trim() ? note.trim() : null,
                        },
                        { onSuccess: closeModal }
                      );
                    }}
                    data-testid="restore-confirm"
                  >
                    Restore as version {config.current_version + 1}
                  </Button>
                </Group>
              </Stack>
            ) : (
              <Group justify="flex-end">
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={closeModal}
                  data-testid="config-version-close"
                >
                  Close
                </Button>
              </Group>
            )}
          </Stack>
        )}
      </Modal>
    </Box>
  );
}

function HistoryRow({
  version,
  isCurrent,
  canWrite,
  onView,
  onRestore,
}: {
  version: AgentConfigVersionSummary;
  isCurrent: boolean;
  canWrite: boolean;
  onView: () => void;
  onRestore: () => void;
}) {
  const findings = version.scan_findings ?? [];

  return (
    <Box
      className={`${classes.historyRow} ${isCurrent ? classes.historyRowCurrent : ''}`}
      data-testid={`config-version-row-${version.version_num}`}
    >
      <Stack gap={6}>
        <Group gap="xs" justify="space-between" wrap="nowrap">
          <Group gap="xs" wrap="wrap">
            <Text size="sm" fw={600}>
              v{version.version_num}
            </Text>
            <Badge
              size="xs"
              variant="light"
              color={EVENT_COLOR[version.event_type]}
            >
              {EVENT_LABEL[version.event_type]}
            </Badge>
            {version.origin === 'copied_from_reported' ? (
              <Tooltip
                label="The body started from what the agent process reported about itself, which is unverified text."
                multiline
                w={260}
              >
                <Badge size="xs" variant="outline" color="orange">
                  from reported
                </Badge>
              </Tooltip>
            ) : null}
            {findings.length > 0 ? (
              <Tooltip
                label={findings.map((finding) => finding.message).join(' · ')}
                multiline
                w={280}
              >
                <Badge
                  size="xs"
                  variant="outline"
                  color="yellow"
                  data-testid={`config-version-findings-${version.version_num}`}
                >
                  {findings.length} finding{findings.length === 1 ? '' : 's'}
                </Badge>
              </Tooltip>
            ) : null}
            {isCurrent ? (
              <Badge size="xs" variant="filled" color="gray">
                current
              </Badge>
            ) : null}
          </Group>
        </Group>

        <Text size="xs" c="dimmed">
          {formatWhen(version.created_at)}
          {version.model_id ? ` · model ${version.model_id}` : ''}
        </Text>

        {version.note ? (
          <Text size="xs" style={{ overflowWrap: 'anywhere' }}>
            {version.note}
          </Text>
        ) : null}

        <Text size="xs" c="dimmed">
          {/* Deliberately "credential" and never "user". The hash identifies an
              API key, and under the default provider every dashboard caller
              hashes to the same value. */}
          credential {version.changed_by_hash ?? 'unrecorded'}
        </Text>

        <Group gap="xs">
          <Button
            variant="ghost"
            size="sm"
            onClick={onView}
            data-testid={`config-version-view-${version.version_num}`}
          >
            View
          </Button>
          {canWrite ? (
            <Button
              variant="ghost"
              size="sm"
              onClick={onRestore}
              data-testid={`config-version-restore-${version.version_num}`}
            >
              Restore
            </Button>
          ) : (
            <Tooltip label="Requires an admin key">
              <span>
                <Button
                  variant="ghost"
                  size="sm"
                  disabled
                  data-testid={`config-version-restore-${version.version_num}`}
                >
                  Restore
                </Button>
              </span>
            </Tooltip>
          )}
        </Group>
      </Stack>
    </Box>
  );
}
