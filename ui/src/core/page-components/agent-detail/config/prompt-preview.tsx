import { Box, Stack, Text } from '@mantine/core';

import classes from './config-tab.module.css';

/**
 * The fence the SDK wraps a managed prompt in, mirrored from
 * `wrap_managed_prompt` in
 * `sdks/python/src/agent_control/integrations/google_adk/_agent_config.py`.
 *
 * Duplicated rather than imported because nothing on this page can import
 * Python. If the two ever drift, this preview lies about what the model gets,
 * which is the one thing it exists not to do, so both sides quote the other.
 */
const MANAGED_PROMPT_TAG = 'agent_control_system_prompt';
const MANAGED_PROMPT_PREAMBLE =
  'The following is operator configuration for this agent, set in Agent ' +
  'Control. Where it conflicts with any earlier instruction in this system ' +
  'message, follow this block.';

const GUIDANCE_TAG = 'agent_control_guidance';

export function wrapManagedPrompt(body: string): string {
  return `<${MANAGED_PROMPT_TAG}>\n${MANAGED_PROMPT_PREAMBLE}\n${body}\n</${MANAGED_PROMPT_TAG}>`;
}

/**
 * What the model actually receives, in the order it receives it.
 *
 * Showing the raw body here would be a lie of omission in two directions. The
 * body is fenced before it is sent, and it is not the only thing in the
 * system message: Google ADK assembles that field from the agent's own
 * declared instruction, its identity block and, for a multi-agent app, the
 * transfer preamble that makes routing work. A saved prompt is appended after
 * all of that rather than replacing it, which was settled by running the real
 * framework, and the block's own first sentence is what states precedence.
 *
 * The framework half is described rather than reproduced. This page has no
 * copy of it and inventing one would be worse than saying so.
 */
export function PromptPreview({ body }: { body: string }) {
  // Wrapped exactly as typed. The server stores the body verbatim and the SDK
  // fences it verbatim, so trimming it here for display would make the preview
  // disagree with what the model gets over the one thing it exists to show.
  const hasBody = body.trim() !== '';

  return (
    <Stack gap="xs">
      <Text size="xs" c="dimmed">
        The system message reaches the model in this order. Agent Control writes
        the middle block; it does not remove what comes before it.
      </Text>

      <Box className={classes.preview}>
        <Stack gap="sm">
          <Stack gap={2}>
            <Text className={classes.previewLabel} c="dimmed">
              1. Assembled by the agent&apos;s own code and by Google ADK
            </Text>
            <Text size="xs" c="dimmed">
              The instruction declared in the agent&apos;s source, its identity
              block, and for a multi-agent app the transfer preamble that lets
              it hand work to a sub-agent. Agent Control does not hold a copy of
              this text, so it is described here rather than shown.
            </Text>
          </Stack>

          <Stack gap={2}>
            <Text className={classes.previewLabel} c="dimmed">
              2. Your saved prompt, fenced
            </Text>
            <pre
              className={classes.bodyText}
              data-testid="prompt-preview-block"
            >
              {hasBody
                ? wrapManagedPrompt(body)
                : 'Nothing saved yet, so nothing is added here.'}
            </pre>
          </Stack>

          <Stack gap={2}>
            <Text className={classes.previewLabel} c="dimmed">
              3. Control guidance, when a control steers this call
            </Text>
            <Text size="xs" c="dimmed">
              Steering text from a control is appended last, inside its own{' '}
              <span className={classes.envVar}>{`<${GUIDANCE_TAG}>`}</span>{' '}
              fence, closest to the model. A saved prompt can never displace it
              or precede it, and a body containing either fence is refused at
              save time so it cannot forge one.
            </Text>
          </Stack>
        </Stack>
      </Box>
    </Stack>
  );
}
