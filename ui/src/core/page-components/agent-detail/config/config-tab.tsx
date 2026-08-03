import {
  Alert,
  Box,
  Center,
  Divider,
  Group,
  Loader,
  Modal,
  Stack,
  Switch,
  Text,
  Textarea,
  Tooltip,
} from '@mantine/core';
import { notifications } from '@mantine/notifications';
import { Button } from '@rungalileo/jupiter-ds';
import {
  IconAlertCircle,
  IconAlertTriangle,
  IconInfoCircle,
  IconLock,
} from '@tabler/icons-react';
import {
  type MutableRefObject,
  useCallback,
  useEffect,
  useMemo,
  useState,
} from 'react';

import { isConflictError, isForbiddenError } from '@/core/api/errors';
import type { ScanFinding } from '@/core/api/types';
import {
  CONFIG_NOTE_MAX_LENGTH,
  CONFIG_PICKUP_COPY,
  PROMPT_BODY_MAX_LENGTH,
  useAgentConfig,
} from '@/core/hooks/query-hooks/use-agent-config';
import {
  useAgentModels,
  useCanWriteAgentConfig,
} from '@/core/hooks/query-hooks/use-agent-models';
import {
  useClearAgentModel,
  useClearAgentPrompt,
  useSetPromptEnabled,
  useUpdateAgentConfig,
} from '@/core/hooks/query-hooks/use-update-agent-config';

import { ConfigHistory } from './config-history';
import classes from './config-tab.module.css';
import { ModelSelect } from './model-select';
import { PromptEditor } from './prompt-editor';
import {
  UNSAVED_CHANGES_MESSAGE,
  useUnsavedChangesGuard,
} from './use-unsaved-changes-guard';

/**
 * What the agent detail page needs from this tab to guard its own tab switch.
 *
 * The tab list lives a level up and pushes a route on change, so it has to ask
 * before it does that. Registered through a ref, the way the edit-control
 * modal already does it on this page.
 */
export type ConfigTabGuard = {
  requestExit: (proceed: () => void) => void;
};

type ConfigTabProps = {
  agentName: string;
  guardRef?: MutableRefObject<ConfigTabGuard | null>;
};

function errorMessage(error: unknown, fallback: string): string {
  if (isForbiddenError(error)) {
    return 'That needs an admin key. Sign in with one and try again.';
  }
  return error instanceof Error ? error.message : fallback;
}

function ScanFindingsAlert({ findings }: { findings: ScanFinding[] }) {
  return (
    <Alert
      icon={<IconAlertTriangle size={16} />}
      color="yellow"
      variant="light"
      title={`Saved, with ${findings.length} thing${findings.length === 1 ? '' : 's'} worth a look`}
      data-testid="config-scan-findings"
    >
      <Stack gap={4}>
        {findings.map((finding, index) => (
          <Text size="sm" key={`${index}-${finding.scanner}-${finding.code}`}>
            {finding.message}
          </Text>
        ))}
        <Text size="xs" c="dimmed">
          Advisory only. The save went through, and the findings are recorded on
          the version row so the history shows that somebody saw them.
        </Text>
      </Stack>
    </Alert>
  );
}

/**
 * The Configuration tab: one agent's system prompt and its model.
 *
 * Two fields, one row, one version counter, one Save. They are one save
 * because they are one row on the server: a prompt edit and a model edit
 * conflict with each other, which is correct, because they are one version.
 *
 * Nothing here is instant and the copy never says it is. A save reaches a
 * running agent when that agent next polls, roughly a minute on the shipped
 * defaults, and takes effect at its next model call after that. A model call
 * already dispatched is untouched, and a turn that is running can make its
 * first call on one model and its second on another.
 */
export function ConfigTab({ agentName, guardRef }: ConfigTabProps) {
  const configQuery = useAgentConfig(agentName);
  const modelsQuery = useAgentModels();
  const { canWrite } = useCanWriteAgentConfig();

  const save = useUpdateAgentConfig(agentName);
  const clearPrompt = useClearAgentPrompt(agentName);
  const clearModel = useClearAgentModel(agentName);
  const setPromptEnabled = useSetPromptEnabled(agentName);

  // Edits are held as an overlay on the server's row rather than as a copy of
  // it. Null means "no edit here", so the field follows whatever the server
  // last returned without an effect to keep the two in step: a refetch after
  // somebody else's save updates an untouched field and leaves a field being
  // typed in alone. A genuinely concurrent write is caught by
  // `expected_version` and surfaced as a conflict, which is the only honest
  // answer once both sides have changed.
  const [promptEdit, setPromptEdit] = useState<string | null>(null);
  const [modelEdit, setModelEdit] = useState<{ value: string | null } | null>(
    null
  );
  const [noteDraft, setNoteDraft] = useState('');
  const [findings, setFindings] = useState<ScanFinding[] | null>(null);
  const [clearing, setClearing] = useState<'prompt' | 'model' | null>(null);

  const config = configQuery.data;
  const models = useMemo(
    () => modelsQuery.data?.models ?? [],
    [modelsQuery.data?.models]
  );

  const storedBody = config?.body ?? '';
  const storedModel = config?.model_id ?? null;

  const bodyDraft = promptEdit ?? storedBody;
  const modelDraft = modelEdit ? modelEdit.value : storedModel;

  const fieldsDirty =
    Boolean(config) && (bodyDraft !== storedBody || modelDraft !== storedModel);
  const dirty = fieldsDirty || noteDraft.trim().length > 0;

  const discard = useCallback(() => {
    setPromptEdit(null);
    setModelEdit(null);
    setNoteDraft('');
  }, []);

  const setModelDraft = useCallback((value: string | null) => {
    setModelEdit({ value });
  }, []);

  const guard = useUnsavedChangesGuard(dirty, discard);

  useEffect(() => {
    if (!guardRef) return;
    guardRef.current = { requestExit: guard.requestExit };
    return () => {
      guardRef.current = null;
    };
  }, [guardRef, guard.requestExit]);

  if (configQuery.isPending) {
    return (
      <Center py="xl">
        <Stack align="center" gap="sm">
          <Loader size="sm" />
          <Text size="sm" c="dimmed">
            Loading this agent&apos;s configuration…
          </Text>
        </Stack>
      </Center>
    );
  }

  if (configQuery.isError || !config) {
    return (
      <Alert
        icon={<IconAlertCircle size={16} />}
        color="red"
        variant="light"
        title="Configuration could not be loaded"
        data-testid="config-load-error"
      >
        <Text size="sm">
          {errorMessage(
            configQuery.error,
            'The configuration for this agent did not load.'
          )}
        </Text>
      </Alert>
    );
  }

  const editable = canWrite !== false;
  const inputsEditable = editable && !save.isPending;
  const allowlistForbidden = isForbiddenError(modelsQuery.error);
  // A 403 is an answer: this key may not enumerate the allowlist, and the
  // read-only rendering is correct. Anything else is a failed request, and the
  // picker must not turn that into a claim about how the server is configured.
  const allowlistUnavailable =
    Boolean(modelsQuery.error) && !allowlistForbidden;

  const bodyBlankButStored = bodyDraft.trim() === '' && storedBody !== '';
  const modelClearedInDraft = modelDraft === null && storedModel !== null;
  const bodyTooLong = bodyDraft.length > PROMPT_BODY_MAX_LENGTH;

  const blockingReason = bodyBlankButStored
    ? 'Emptying the box is not how a prompt is removed. Use Clear prompt, which records the removal in the history and hands the instruction back to the agent’s code.'
    : modelClearedInDraft
      ? 'Use Clear model to hand the model choice back to the agent’s code.'
      : null;

  const canSave =
    editable &&
    fieldsDirty &&
    !bodyTooLong &&
    !blockingReason &&
    !save.isPending;

  const conflict = isConflictError(save.error);

  const handleSave = () => {
    const note = noteDraft.trim();
    // Findings belong to one save. Leaving the previous save's alert up while
    // this one is in flight, or after it fails, would show "Saved, with N
    // things worth a look" over a write that did not happen.
    setFindings(null);
    save.mutate(
      {
        body: bodyDraft !== storedBody ? bodyDraft : undefined,
        model_id:
          modelDraft && modelDraft !== storedModel ? modelDraft : undefined,
        expected_version: config.current_version,
        // Preserved rather than defaulted. Saving new text on a prompt whose
        // delivery is switched off must not switch it back on: that would be a
        // behaviour change nobody asked for, hidden inside a text edit.
        prompt_enabled: config.prompt_enabled,
        note: note ? note : null,
      },
      {
        onSuccess: (result) => {
          setNoteDraft('');
          setFindings(
            result.scan_findings && result.scan_findings.length > 0
              ? result.scan_findings
              : null
          );
          notifications.show({
            title: `Saved as version ${result.version_num}`,
            message: `${CONFIG_PICKUP_COPY}. A model call already in flight is not affected.`,
            color: 'green',
          });
        },
      }
    );
  };

  const restorePromptOnly = (body: string) => {
    save.mutate(
      {
        body,
        expected_version: config.current_version,
        prompt_enabled: config.prompt_enabled,
        note: 'Prompt text restored from an earlier version; model left as configured.',
      },
      {
        onSuccess: (result) => {
          discard();
          notifications.show({
            title: `Saved as version ${result.version_num}`,
            message: `The prompt text is back and the model is unchanged. ${CONFIG_PICKUP_COPY}.`,
            color: 'green',
          });
        },
      }
    );
  };

  const runClear = () => {
    if (clearing === 'prompt') {
      clearPrompt.mutate(
        { expected_version: config.current_version },
        {
          onSuccess: (result) => {
            // The overlay has to go, not be set to the new value: the field it
            // was covering is now null on the server, and an edit that still
            // held the old text would show a prompt that is no longer stored.
            discard();
            setClearing(null);
            notifications.show({
              title: result.cleared
                ? `Prompt cleared in version ${result.version_num}`
                : 'There was no managed prompt to clear',
              message: result.cleared
                ? `This agent goes back to the instruction its code declares. ${CONFIG_PICKUP_COPY}.`
                : 'Nothing was written.',
              color: 'blue',
            });
          },
        }
      );
      return;
    }
    clearModel.mutate(
      { expected_version: config.current_version },
      {
        onSuccess: (result) => {
          discard();
          setClearing(null);
          notifications.show({
            title: result.cleared
              ? `Model cleared in version ${result.version_num}`
              : 'There was no managed model to clear',
            message: result.cleared
              ? `This agent goes back to the model its code declares. ${CONFIG_PICKUP_COPY}.`
              : 'Nothing was written.',
            color: 'blue',
          });
        },
      }
    );
  };

  const gateBlocked = config.delivery_state === 'blocked_insecure_auth';
  // The prompt can only resolve to "managed" on a server where delivery is
  // allowed, so a blocked state alongside a managed prompt can only be the
  // local-development override's economy-tier cap on the model half.
  const tierLimited = gateBlocked && config.prompt_source === 'managed';

  return (
    <Stack gap="lg" data-testid="agent-config-tab">
      {gateBlocked ? (
        <Alert
          icon={<IconLock size={16} />}
          color="orange"
          variant="light"
          title={
            tierLimited
              ? 'The model on this page is not being delivered'
              : 'Saved here, not delivered to the agent'
          }
          data-testid="config-delivery-blocked"
        >
          {tierLimited ? (
            <Stack gap={4}>
              <Text size="sm">
                The prompt is being delivered. The model is not: this server has
                credential enforcement off with the local development override
                on, and that combination applies economy-tier models only.
              </Text>
              <Text size="xs" c="dimmed">
                The agent keeps calling the model its own code declares. Set{' '}
                <span className={classes.envVar}>
                  AGENT_CONTROL_API_KEY_ENABLED=true
                </span>{' '}
                with a key to lift the cap.
              </Text>
            </Stack>
          ) : (
            <Stack gap={4}>
              <Text size="sm">
                This server is running with credential enforcement off, so every
                operation, admin ones included, succeeds unauthenticated.
                Nothing on this tab is applied to a running agent while that is
                true.
              </Text>
              <Text size="xs" c="dimmed">
                Editing, versioning and the audit trail all keep working. Set{' '}
                <span className={classes.envVar}>
                  AGENT_CONTROL_API_KEY_ENABLED=true
                </span>{' '}
                with a key to turn delivery on. On a development machine{' '}
                <span className={classes.envVar}>
                  AGENT_CONTROL_AGENT_CONFIG_ALLOW_INSECURE_LOCAL_DEV=true
                </span>{' '}
                delivers the prompt and economy-tier models only.
              </Text>
            </Stack>
          )}
        </Alert>
      ) : null}

      {canWrite === false ? (
        <Alert
          icon={<IconInfoCircle size={16} />}
          color="gray"
          variant="light"
          title="Read-only"
          data-testid="config-read-only"
        >
          <Text size="sm">
            This key can read an agent&apos;s configuration and its history.
            Saving, clearing and restoring need an admin key.
          </Text>
        </Alert>
      ) : null}

      {config.current_version === 0 ? (
        <Alert
          icon={<IconInfoCircle size={16} />}
          color="blue"
          variant="light"
          title="Nothing configured for this agent yet"
          data-testid="config-empty-state"
        >
          <Stack gap={4}>
            <Text size="sm">
              This agent runs the instruction and the model declared in its own
              code. Saving a model here replaces the code&apos;s choice on every
              turn. Saving a prompt adds your text to the system message after
              what the code and the framework assembled, with a line telling the
              model to prefer your block where the two conflict.
            </Text>
            <Text size="xs" c="dimmed">
              A prompt cannot replace the code&apos;s instruction outright: the
              same field carries the agent&apos;s identity block and, for a
              multi-agent app, the preamble that makes handing work to a
              sub-agent work at all. Clearing either field hands it straight
              back to the code.
            </Text>
          </Stack>
        </Alert>
      ) : null}

      {findings ? <ScanFindingsAlert findings={findings} /> : null}

      {save.isError ? (
        <Alert
          icon={<IconAlertCircle size={16} />}
          color={conflict ? 'orange' : 'red'}
          variant="light"
          title={
            conflict
              ? 'Somebody else saved while this was open'
              : 'The configuration was not saved'
          }
          data-testid="config-save-error"
        >
          <Stack gap="xs">
            <Text size="sm">
              {errorMessage(save.error, 'The save was refused.')}
            </Text>
            {conflict ? (
              <Group gap="xs">
                <Text size="xs" c="dimmed">
                  Your edits are still in the box. Reload to see what changed,
                  then apply them again.
                </Text>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => void configQuery.refetch()}
                  data-testid="config-conflict-reload"
                >
                  Reload the saved version
                </Button>
              </Group>
            ) : null}
          </Stack>
        </Alert>
      ) : null}

      <Box className={classes.columns}>
        <Stack gap="lg">
          <Box className={classes.panel}>
            <Box className={classes.panelBody}>
              <Stack gap="md">
                <ModelSelect
                  value={modelDraft}
                  onChange={setModelDraft}
                  models={models}
                  config={config}
                  editable={inputsEditable}
                  allowlistForbidden={allowlistForbidden}
                  allowlistUnavailable={allowlistUnavailable}
                />

                <Group gap="md" justify="space-between" align="center">
                  {/* What the agent is calling now, which is not always what
                      the picker shows: an id that left the allowlist, a gated
                      server or the local-dev tier cap all leave the code's
                      choice in charge, and the row above says which. */}
                  <Text size="xs" c="dimmed" data-testid="model-in-effect">
                    {config.model_source === 'managed'
                      ? 'Calling the model saved here.'
                      : 'Calling the model declared in the agent’s own code.'}
                  </Text>
                  {storedModel !== null && editable ? (
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => setClearing('model')}
                      data-testid="clear-model-button"
                    >
                      Clear model
                    </Button>
                  ) : null}
                </Group>

                <Divider />

                <PromptEditor
                  value={bodyDraft}
                  onChange={setPromptEdit}
                  editable={inputsEditable}
                  hasStoredBody={storedBody !== ''}
                />

                {storedBody !== '' ? (
                  <Group gap="md" justify="space-between" align="flex-start">
                    <Switch
                      checked={config.prompt_enabled}
                      onChange={(event) =>
                        setPromptEnabled.mutate({
                          prompt_enabled: event.currentTarget.checked,
                          expected_version: config.current_version,
                        })
                      }
                      disabled={!editable || setPromptEnabled.isPending}
                      label="Deliver this prompt to the agent"
                      description="Switching it off keeps the text and the history, and the agent goes back to what its code declares."
                      data-testid="prompt-enabled-switch"
                    />
                    {editable ? (
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => setClearing('prompt')}
                        data-testid="clear-prompt-button"
                      >
                        Clear prompt
                      </Button>
                    ) : null}
                  </Group>
                ) : null}

                {setPromptEnabled.isError ? (
                  <Text size="xs" c="red" data-testid="prompt-enabled-error">
                    {errorMessage(
                      setPromptEnabled.error,
                      'Prompt delivery was not changed.'
                    )}
                  </Text>
                ) : null}

                <Textarea
                  label="Note"
                  description="Optional. Recorded on the version row, next to who changed it."
                  placeholder="What this change is for"
                  value={noteDraft}
                  maxLength={CONFIG_NOTE_MAX_LENGTH}
                  onChange={(event) => setNoteDraft(event.currentTarget.value)}
                  readOnly={!editable}
                  autosize
                  minRows={2}
                  data-testid="config-note-input"
                />

                {blockingReason ? (
                  <Text size="sm" c="orange" data-testid="config-blocking-hint">
                    {blockingReason}
                  </Text>
                ) : null}

                <Group justify="space-between" align="center">
                  <Text size="xs" c="dimmed">
                    {CONFIG_PICKUP_COPY}, then at that agent&apos;s next model
                    call. A call already sent is not affected, and a turn that
                    is already running can make its first call on one model and
                    its next on another.
                  </Text>
                  <Group gap="xs" wrap="nowrap">
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={discard}
                      disabled={!dirty || save.isPending}
                      data-testid="config-discard-button"
                    >
                      Discard changes
                    </Button>
                    {editable ? (
                      <Button
                        variant="filled"
                        size="sm"
                        onClick={handleSave}
                        disabled={!canSave}
                        loading={save.isPending}
                        data-testid="config-save-button"
                      >
                        Save
                      </Button>
                    ) : (
                      <Tooltip label="Requires an admin key">
                        <span>
                          <Button
                            variant="filled"
                            size="sm"
                            disabled
                            data-testid="config-save-button"
                          >
                            Save
                          </Button>
                        </span>
                      </Tooltip>
                    )}
                  </Group>
                </Group>
              </Stack>
            </Box>
          </Box>
        </Stack>

        <ConfigHistory
          agentName={agentName}
          config={config}
          canWrite={editable}
          onRestorePromptOnly={restorePromptOnly}
          restorePromptOnlyPending={save.isPending}
        />
      </Box>

      <Modal
        opened={guard.askOpen}
        onClose={guard.cancelDiscard}
        title="Leave without saving?"
        styles={{ title: { fontWeight: 600 } }}
      >
        <Stack gap="md">
          <Text size="sm">{UNSAVED_CHANGES_MESSAGE}</Text>
          <Group justify="flex-end" gap="xs">
            <Button
              variant="ghost"
              size="sm"
              onClick={guard.cancelDiscard}
              data-testid="unsaved-stay"
            >
              Stay here
            </Button>
            <Button
              variant="filled"
              size="sm"
              onClick={guard.confirmDiscard}
              data-testid="unsaved-discard"
            >
              Discard and leave
            </Button>
          </Group>
        </Stack>
      </Modal>

      <Modal
        opened={clearing !== null}
        onClose={() => setClearing(null)}
        title={clearing === 'model' ? 'Clear the model?' : 'Clear the prompt?'}
        styles={{ title: { fontWeight: 600 } }}
      >
        <Stack gap="md">
          <Text size="sm">
            {clearing === 'model'
              ? 'This agent goes back to calling the model declared in its own code.'
              : 'This agent goes back to the instruction declared in its own code, and prompt delivery is switched off.'}
          </Text>
          <Text size="xs" c="dimmed">
            Clearing is recorded as a new version and the text is kept in the
            history, so it can be restored later. {CONFIG_PICKUP_COPY}.
          </Text>
          {clearPrompt.isError || clearModel.isError ? (
            <Text size="sm" c="red" data-testid="config-clear-error">
              {errorMessage(
                clearPrompt.error ?? clearModel.error,
                'Nothing was cleared.'
              )}
            </Text>
          ) : null}
          <Group justify="flex-end" gap="xs">
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setClearing(null)}
              data-testid="config-clear-cancel"
            >
              Cancel
            </Button>
            <Button
              variant="filled"
              size="sm"
              onClick={runClear}
              loading={clearPrompt.isPending || clearModel.isPending}
              data-testid="config-clear-confirm"
            >
              {clearing === 'model' ? 'Clear model' : 'Clear prompt'}
            </Button>
          </Group>
        </Stack>
      </Modal>
    </Stack>
  );
}
