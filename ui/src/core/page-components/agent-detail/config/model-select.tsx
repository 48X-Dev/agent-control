import { Alert, Badge, Group, Select, Stack, Text } from '@mantine/core';
import { IconAlertTriangle } from '@tabler/icons-react';
import { useMemo } from 'react';

import type {
  AgentModelOption,
  GetAgentConfigResponse,
  ModelCostTier,
} from '@/core/api/types';

const COST_TIER_COLOR: Record<ModelCostTier, string> = {
  economy: 'gray',
  standard: 'blue',
  premium: 'grape',
};

/**
 * The operator's own spend banding, passed through as they wrote it.
 *
 * No currency and no per-token figure anywhere on this tab. Agent Control does
 * not know prices, prices change without telling it, and a wrong number beside
 * a Save button is worse than no number.
 */
export function CostTierBadge({ tier }: { tier: ModelCostTier }) {
  return (
    <Badge
      size="xs"
      variant="light"
      color={COST_TIER_COLOR[tier]}
      data-testid={`model-cost-tier-${tier}`}
    >
      {tier}
    </Badge>
  );
}

type ModelSelectProps = {
  /** The id currently in the editor, which may differ from what is stored. */
  value: string | null;
  onChange: (value: string | null) => void;
  models: AgentModelOption[];
  config: GetAgentConfigResponse;
  /** False for a read-only viewer, and while a save is in flight. */
  editable: boolean;
  /** True when the allowlist could not be read because the key is not admin. */
  allowlistForbidden: boolean;
  /**
   * True when the allowlist request failed for any reason other than a 403.
   *
   * It has to be told apart from an empty list. An empty list is a statement
   * about the server; a failed request is a statement about nothing, and
   * rendering it as "no models configured" sends an operator to edit an env var
   * that is probably already correct.
   */
  allowlistUnavailable: boolean;
};

/**
 * Which model this agent calls.
 *
 * A `Select` over a server-configured allowlist rather than a text field with
 * suggestions. Free text on a field with an allowlist teaches people to type
 * and then punishes them with a 400, and worse: a model id is a destination
 * selector, not a name. A slash prefix re-selects the vendor and a configured
 * endpoint is ignored for routing, so an id nobody vetted can send every
 * prompt and tool result to a host of the writer's choosing.
 *
 * There is no endpoint field here and there is no column for one. A different
 * endpoint means a different executor process with a different environment.
 */
export function ModelSelect({
  value,
  onChange,
  models,
  config,
  editable,
  allowlistForbidden,
  allowlistUnavailable,
}: ModelSelectProps) {
  const byId = useMemo(
    () => new Map(models.map((model) => [model.id, model])),
    [models]
  );

  // Whether the stored id has left the allowlist is the *server's* answer, not
  // a membership test against the list this page happens to hold. Re-deriving
  // it here would turn any failure of the allowlist request into a confident
  // claim that the agent has fallen back to its code, contradicting the
  // `model_source` the same response just supplied.
  //
  // The row is never rewritten and the picker never silently corrects it: an
  // operator who mistyped one line of server config should not lose model
  // choices across a namespace with no version row recording it.
  const storedIsMissing = Boolean(config.model_id) && !config.model_allowed;

  // Separate question: can the picker show the stored id at all? Only if the
  // list it was populated from carries it.
  const storedIsUnlisted = Boolean(
    config.model_id && !byId.has(config.model_id)
  );

  const data = useMemo(() => {
    const options = models.map((model) => ({
      value: model.id,
      label: model.label,
    }));
    if (storedIsUnlisted && config.model_id) {
      options.unshift({
        value: config.model_id,
        label: `${config.model_id} (not available)`,
        disabled: true,
      } as (typeof options)[number]);
    }
    return options;
  }, [models, storedIsUnlisted, config.model_id]);

  const selected = value ? byId.get(value) : undefined;

  // A read-only viewer gets no picker, because the allowlist route is gated on
  // the write operation and there is nothing to populate one with. What they
  // do see is their own agent's model and its tier, which come back on the
  // per-agent read.
  if (!editable && allowlistForbidden) {
    return (
      <Stack gap={4} data-testid="model-select-readonly">
        <Text size="sm" fw={500}>
          Model
        </Text>
        {config.model_id ? (
          <Group gap="xs">
            <Text size="sm" data-testid="model-readonly-id">
              {config.model_id}
            </Text>
            {config.model_cost_tier ? (
              <CostTierBadge tier={config.model_cost_tier} />
            ) : null}
            {!config.model_allowed ? (
              <Badge size="xs" variant="light" color="orange">
                Not available
              </Badge>
            ) : null}
          </Group>
        ) : (
          <Text size="sm" c="dimmed">
            Whatever the agent&apos;s code declares.
          </Text>
        )}
        <Text size="xs" c="dimmed">
          Listing the models this server offers needs an admin key.
        </Text>
      </Stack>
    );
  }

  // A failed request is not an answer. Saying "no models configured" here
  // would send an operator to edit server config over what is probably a
  // transient error, and it is the one sentence on this tab that reads like a
  // statement about the deployment.
  if (allowlistUnavailable) {
    return (
      <Stack gap={4} data-testid="model-select-unavailable">
        <Text size="sm" fw={500}>
          Model
        </Text>
        <Group gap="xs">
          <Text size="sm">
            {config.model_id ?? 'Whatever the agent’s code declares.'}
          </Text>
          {config.model_cost_tier ? (
            <CostTierBadge tier={config.model_cost_tier} />
          ) : null}
        </Group>
        <Text size="xs" c="dimmed">
          The list of models this server offers did not load, so there is
          nothing to pick from right now. What is stored is shown above and is
          unaffected. Reload the page to try again.
        </Text>
      </Stack>
    );
  }

  if (models.length === 0 && !storedIsUnlisted) {
    return (
      <Stack gap={4} data-testid="model-select-empty-allowlist">
        <Text size="sm" fw={500}>
          Model
        </Text>
        <Text size="sm" c="dimmed">
          No models configured on this server.
        </Text>
        <Text size="xs" c="dimmed">
          An operator sets the allowlist with{' '}
          <span>AGENT_CONTROL_MODELS_ALLOWLIST</span> and restarts the server.
          Until then every agent runs the model its own code declares, and the
          system prompt below still works normally.
        </Text>
      </Stack>
    );
  }

  return (
    <Stack gap="xs" data-testid="model-select">
      <Select
        label="Model"
        description="Chosen from the models this server offers. Clearing it hands the choice back to the agent's code."
        placeholder="Whatever the agent's code declares"
        data={data}
        value={value}
        onChange={onChange}
        disabled={!editable}
        searchable={models.length > 8}
        nothingFoundMessage="No model matches that."
        data-testid="model-select-input"
        renderOption={({ option }) => {
          const model = byId.get(option.value);
          return (
            <Group gap="xs" wrap="nowrap" justify="space-between" w="100%">
              <Stack gap={0}>
                <Text size="sm">{option.label}</Text>
                {model ? (
                  <Text size="xs" c="dimmed">
                    {model.id}
                  </Text>
                ) : null}
              </Stack>
              <Group gap={4} wrap="nowrap">
                {model?.recommended ? (
                  <Badge size="xs" variant="light" color="teal">
                    Recommended
                  </Badge>
                ) : null}
                {model ? (
                  <CostTierBadge tier={model.cost_tier} />
                ) : (
                  <Badge size="xs" variant="light" color="orange">
                    Not available
                  </Badge>
                )}
              </Group>
            </Group>
          );
        }}
      />

      {selected ? (
        <Group gap="xs">
          <Text size="xs" c="dimmed">
            {selected.id}
          </Text>
          <CostTierBadge tier={selected.cost_tier} />
          {selected.recommended ? (
            <Badge size="xs" variant="light" color="teal">
              Recommended
            </Badge>
          ) : null}
        </Group>
      ) : null}

      {storedIsMissing && config.model_id ? (
        <Alert
          icon={<IconAlertTriangle size={16} />}
          color="orange"
          variant="light"
          title="This agent is configured for a model the server no longer offers"
          data-testid="model-not-available-alert"
        >
          <Stack gap={4}>
            <Text size="sm">
              The stored id is {config.model_id}. It is not on this
              server&apos;s allowlist, so the agent is running the model its own
              code declares until somebody picks an available one.
            </Text>
            <Text size="xs" c="dimmed">
              Nothing was rewritten and the history is untouched. Putting that
              entry back in the server&apos;s allowlist restores the previous
              behaviour with no save here.
            </Text>
          </Stack>
        </Alert>
      ) : null}
    </Stack>
  );
}
