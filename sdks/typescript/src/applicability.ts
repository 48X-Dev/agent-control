/**
 * Control prefiltering, ported from the Python engine's
 * `ControlEngine.get_applicable_controls` (engine/src/agent_control_engine/core.py).
 *
 * The point of prefiltering here is NOT to skip work. It is to know, before
 * making a network call, whether any control could apply to this step - and, if
 * one applies that this SDK cannot honour, to say so instead of quietly
 * dropping it.
 */
import type { ConditionNodeOutput } from "./generated/models/condition-node-output";
import type { Control } from "./generated/models/control";
import type { ControlDefinitionOutput } from "./generated/models/control-definition-output";
import type { ControlStage } from "./errors";

/** A control from the cache that has a concrete, evaluable definition. */
export interface RenderedControl {
  id: number;
  name: string;
  definition: ControlDefinitionOutput;
}

export interface StepDescriptor {
  name: string;
  type: string;
  stage: ControlStage;
}

export interface PrefilterResult {
  /** Applicable controls the server will evaluate (`execution: "server"`). */
  serverControls: RenderedControl[];
  /**
   * Applicable controls marked `execution: "sdk"`. The Python SDK runs these
   * in-process; this SDK has no local evaluator engine, so it cannot. They are
   * reported, never silently dropped.
   */
  unsupportedLocalControls: RenderedControl[];
  /** Applicable controls whose scope could not be interpreted. */
  unreadableControls: { control: RenderedControl; problem: string }[];
}

/**
 * Unrendered template controls carry a template but no condition, so there is
 * nothing to evaluate. The server excludes them from the effective set too
 * (they are always `enabled: false`), which is why skipping them is not a
 * fail-open hole.
 */
export function asRenderedControl(entry: Control): RenderedControl | null {
  const definition = entry.control as Partial<ControlDefinitionOutput>;
  if (!definition || definition.condition === undefined || definition.condition === null) {
    return null;
  }
  return {
    id: entry.id,
    name: entry.name,
    definition: definition as ControlDefinitionOutput,
  };
}

function scopeMatchesStepName(
  definition: ControlDefinitionOutput,
  stepName: string,
): { matched: boolean; problem?: string } {
  const scope = definition.scope;
  const names = scope?.stepNames ?? null;
  const pattern = scope?.stepNameRegex ?? null;

  if ((!names || names.length === 0) && !pattern) {
    return { matched: true };
  }

  if (names && names.includes(stepName)) {
    return { matched: true };
  }

  if (pattern) {
    // Python compiles these with RE2 and uses `search`; an unanchored
    // `RegExp.test` is the closest JS equivalent. A pattern that RE2 accepts
    // but JS rejects lands in the `problem` branch and refuses the call rather
    // than silently treating the control as inapplicable.
    try {
      if (new RegExp(pattern).test(stepName)) {
        return { matched: true };
      }
    } catch (error) {
      return {
        matched: false,
        problem: `invalid step_name_regex '${pattern}': ${String(error)}`,
      };
    }
  }

  return { matched: false };
}

function appliesToStep(
  definition: ControlDefinitionOutput,
  step: StepDescriptor,
): { applies: boolean; problem?: string } {
  if (!definition.enabled) {
    return { applies: false };
  }

  const stages = definition.scope?.stages ?? null;
  if (stages && stages.length > 0 && !stages.includes(step.stage)) {
    return { applies: false };
  }

  const stepTypes = definition.scope?.stepTypes ?? null;
  if (stepTypes && stepTypes.length > 0 && !stepTypes.includes(step.type)) {
    return { applies: false };
  }

  const nameMatch = scopeMatchesStepName(definition, step.name);
  if (nameMatch.problem) {
    return { applies: false, problem: nameMatch.problem };
  }
  return { applies: nameMatch.matched };
}

export function prefilterControls(
  controls: Control[],
  step: StepDescriptor,
): PrefilterResult {
  const result: PrefilterResult = {
    serverControls: [],
    unsupportedLocalControls: [],
    unreadableControls: [],
  };

  for (const entry of controls) {
    const rendered = asRenderedControl(entry);
    if (!rendered) {
      continue;
    }

    const verdict = appliesToStep(rendered.definition, step);
    if (verdict.problem) {
      result.unreadableControls.push({ control: rendered, problem: verdict.problem });
      continue;
    }
    if (!verdict.applies) {
      continue;
    }

    if (rendered.definition.execution === "sdk") {
      result.unsupportedLocalControls.push(rendered);
    } else {
      result.serverControls.push(rendered);
    }
  }

  return result;
}

function conditionChildren(node: ConditionNodeOutput): ConditionNodeOutput[] {
  if (node.and) {
    return node.and;
  }
  if (node.or) {
    return node.or;
  }
  if (node.not) {
    return [node.not];
  }
  return [];
}

export interface ObservabilityIdentity {
  selectorPath: string | null;
  evaluatorName: string | null;
  leafCount: number;
  allEvaluators: string[];
  allSelectorPaths: string[];
}

/**
 * Port of `_build_observability_identity` in models/controls.py: left-to-right
 * leaf traversal, first leaf wins as the representative identity.
 */
export function observabilityIdentity(
  definition: ControlDefinitionOutput,
): ObservabilityIdentity {
  const allEvaluators: string[] = [];
  const allSelectorPaths: string[] = [];
  let leafCount = 0;

  const visit = (node: ConditionNodeOutput): void => {
    const children = conditionChildren(node);
    if (children.length === 0) {
      if (!node.selector || !node.evaluator) {
        return;
      }
      leafCount += 1;
      const selectorPath = node.selector.path || "*";
      if (!allEvaluators.includes(node.evaluator.name)) {
        allEvaluators.push(node.evaluator.name);
      }
      if (!allSelectorPaths.includes(selectorPath)) {
        allSelectorPaths.push(selectorPath);
      }
      return;
    }
    for (const child of children) {
      visit(child);
    }
  };

  visit(definition.condition);

  return {
    selectorPath: allSelectorPaths[0] ?? null,
    evaluatorName: allEvaluators[0] ?? null,
    leafCount,
    allEvaluators,
    allSelectorPaths,
  };
}
