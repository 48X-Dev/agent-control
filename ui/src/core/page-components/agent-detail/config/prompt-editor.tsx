import { Group, Spoiler, Stack, Text, Textarea } from '@mantine/core';

import {
  PROMPT_BODY_MAX_LENGTH,
  PROMPT_BODY_WARN_AT,
} from '@/core/hooks/query-hooks/use-agent-config';

import { PromptPreview } from './prompt-preview';

/**
 * Rough token count, labelled as rough everywhere it appears.
 *
 * Four characters per token is a rule of thumb, not a measurement, and no
 * tokeniser runs in this browser. The number is here to stop somebody pasting
 * a novel, not to predict a bill.
 */
function approximateTokens(text: string): number {
  return Math.ceil(text.length / 4);
}

type PromptEditorProps = {
  value: string;
  onChange: (value: string) => void;
  editable: boolean;
  /** True once the stored body is non-empty, which changes the helper copy. */
  hasStoredBody: boolean;
};

/**
 * The system prompt, as prose.
 *
 * A plain `Textarea` rather than Monaco or CodeMirror, both of which this repo
 * has and uses for the JSON control payloads they are right for. A system
 * prompt is prose: syntax highlighting has nothing to highlight, and a code
 * editor's keybindings fight prose over wrapping and indentation.
 */
export function PromptEditor({
  value,
  onChange,
  editable,
  hasStoredBody,
}: PromptEditorProps) {
  const length = value.length;
  const overLimit = length > PROMPT_BODY_MAX_LENGTH;
  const nearLimit = !overLimit && length >= PROMPT_BODY_WARN_AT;

  return (
    <Stack gap="xs">
      <Textarea
        label="System prompt"
        description="Operator configuration for this agent. It is added to the system message the model sees, after whatever the agent's own code declares."
        placeholder={
          editable
            ? 'Write the instructions this agent should follow.'
            : undefined
        }
        value={value}
        onChange={(event) => onChange(event.currentTarget.value)}
        readOnly={!editable}
        autosize
        minRows={16}
        maxRows={40}
        spellCheck={false}
        error={
          overLimit
            ? `${length.toLocaleString()} characters is past the ${PROMPT_BODY_MAX_LENGTH.toLocaleString()} the server accepts.`
            : undefined
        }
        styles={{
          input: {
            fontFamily: 'var(--mantine-font-family-monospace)',
            fontSize: 'var(--mantine-font-size-sm)',
          },
        }}
        data-testid="prompt-editor-input"
      />

      <Group justify="space-between" gap="xs" align="flex-start">
        <Text
          size="xs"
          c={overLimit ? 'red' : nearLimit ? 'orange' : 'dimmed'}
          data-testid="prompt-editor-counter"
        >
          {length.toLocaleString()} of {PROMPT_BODY_MAX_LENGTH.toLocaleString()}{' '}
          characters · roughly {approximateTokens(value).toLocaleString()}{' '}
          tokens, estimated at four characters per token
        </Text>
      </Group>

      <Text size="xs" c="dimmed">
        Readable by any key in this namespace, including the key each agent
        process uses, and history survives clearing. Do not put credentials
        here. No control evaluates this text: it lands in the system message,
        which guardrails do not read.
      </Text>

      {hasStoredBody || value.trim() ? (
        <Spoiler
          maxHeight={0}
          showLabel="What the model receives"
          hideLabel="Hide what the model receives"
          styles={{ control: { fontSize: 'var(--mantine-font-size-xs)' } }}
        >
          <PromptPreview body={value} />
        </Spoiler>
      ) : null}
    </Stack>
  );
}
