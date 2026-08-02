"""Agent Control plugin integration for Google ADK."""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
import weakref
from collections.abc import Callable, Iterable
from typing import Any, Literal, cast
from uuid import uuid4

from agent_control_models.server import GetAgentResponse

import agent_control
from agent_control import AgentControlClient, agents
from agent_control._control_registry import StepSchemaDict
from agent_control._schema_derivation import derive_schemas
from agent_control._state import state
from agent_control.integrations._core import _evaluate_and_enforce
from agent_control.validation import ensure_agent_name

try:
    from google.adk.agents.callback_context import (  # type: ignore[import-not-found,import-untyped]
        CallbackContext,
    )
    from google.adk.models import (  # type: ignore[import-not-found,import-untyped]
        LlmRequest,
        LlmResponse,
    )
    from google.adk.plugins import BasePlugin  # type: ignore[import-not-found,import-untyped]
    from google.adk.tools import BaseTool  # type: ignore[import-not-found,import-untyped]
    from google.adk.tools.tool_context import (  # type: ignore[import-not-found,import-untyped]
        ToolContext,
    )
except Exception as exc:  # pragma: no cover - optional dependency
    raise RuntimeError(
        "Google ADK integration requires google-adk. "
        "Install with: agent-control-sdk[google-adk]."
    ) from exc

from ._agent_config import ManagedConfigApplier, wrap_guidance
from ._attachments import (
    DEFAULT_HASH_MAX_BYTES,
    AttachmentDescriptor,
    AttachmentScanner,
)
from ._extractors import (
    ExtractedPayload,
    append_operator_turn,
    build_blocked_llm_response,
    build_blocked_tool_response,
    extract_request_payload,
    extract_response_payload,
    resolve_agent_name,
    resolve_tool_agent_name,
    resolve_tool_name,
)
from ._session_state import SessionIdentity
from .nudges import NudgeChannel, build_nudge_text, nudge_step_name

logger = logging.getLogger(__name__)

_ALL_HOOKS = {"before_model", "after_model", "before_tool", "after_tool"}
_SYNC_TIMEOUT_SECONDS = 30

# Session-state keys the server seeds the per-turn attachment manifest under.
# Both shapes are read because ADK state is a flat map that tolerates dotted
# keys and the server writes a nested block; neither is worth a coin toss.
_STATE_BLOCK_KEY = "agent_control"
_STATE_MANIFEST_KEY = "attachment_manifest"
_STATE_MANIFEST_DOTTED_KEY = "agent_control.attachment_manifest"

# Bounded per-invocation bookkeeping. ADK gives no "invocation ended" hook, so
# these are capped and evicted oldest-first rather than trusted to drain. The
# scanner cap is small on purpose: a scanner's hash memo pins the blob objects
# it keyed on, so a generous cap would be a generous memory retention promise.
_MAX_TRACKED_INVOCATIONS = 8
_MAX_WARNED_ATTACHMENTS = 256

# Hand-written refusal text. These are structural refusals by the SDK, not
# guardrail verdicts, so they never travel through blocked_message_template.
_FILE_DATA_BLOCKED_MESSAGE = (
    "Agent Control refused this request: it carries a file reference whose bytes "
    "are fetched by the model provider and can never be read by any control in "
    "this deployment. Attach the file itself instead of a link to it."
)
_UNMINTED_BLOCKED_MESSAGE = (
    "Agent Control refused this request: it carries a file this control plane did "
    "not issue, so no control has seen what is in it."
)


def _state_lookup(state: Any, key: str) -> Any:
    """Read one key from an ADK state object without ever raising.

    ``CallbackContext.state`` is a mapping-like whose exact type is ADK's
    business. A manifest that cannot be read is a manifest that is absent, and
    absent fails closed at ``source="unknown"``; it is never a reason to fail a
    model call.
    """

    try:
        getter = getattr(state, "get", None)
        if callable(getter):
            return getter(key)
        return state[key]
    except Exception:
        logger.debug("Could not read Google ADK session state", exc_info=True)
        return None


class AgentControlPlugin(BasePlugin):
    """Google ADK plugin that enforces Agent Control across model and tool hooks."""

    name = "agent-control-google-adk"

    def __init__(
        self,
        agent_name: str,
        *,
        enabled_hooks: set[str] | None = None,
        blocked_message_template: str | None = None,
        step_name_overrides: dict[str, str] | None = None,
        step_name_resolver: Callable[..., str | None] | None = None,
        context_extractor: Callable[..., dict[str, Any] | None] | None = None,
        on_violation_callback: Callable[[dict[str, Any], Any], None] | None = None,
        enable_logging: bool = True,
        attachment_placeholder_text: bool = True,
        file_data_parts: Literal["allow", "block"] = "block",
        unminted_file_parts: Literal["allow", "warn", "block"] = "warn",
        attachment_hash_max_bytes: int = DEFAULT_HASH_MAX_BYTES,
        wrap_managed_prompt: bool = True,
    ) -> None:
        try:
            # Galileo's local ADK plugin code uses BasePlugin(name=...), but keep
            # a fallback for older/mock BasePlugin implementations that only
            # accept a no-arg constructor.
            super().__init__(name=self.name)
        except TypeError:
            super().__init__()

        normalized_name = ensure_agent_name(agent_name)
        current = state.current_agent
        if current is not None and current.agent_name != normalized_name:
            raise ValueError(
                "AgentControlPlugin agent_name must match the currently initialized "
                "agent_control agent."
            )

        self.agent_name = normalized_name
        self.enabled_hooks = set(enabled_hooks or _ALL_HOOKS)
        self.blocked_message_template = blocked_message_template
        self.step_name_overrides = dict(step_name_overrides or {})
        self.step_name_resolver = step_name_resolver
        self.context_extractor = context_extractor
        self.on_violation_callback = on_violation_callback
        self.enable_logging = enable_logging
        self.attachment_placeholder_text = attachment_placeholder_text
        # The two file defaults differ on purpose. A file_data URI is
        # dereferenced by the model provider, so those bytes are structurally
        # unevaluatable under every configuration this product could ever have:
        # blocking is the only honest answer and it is on by default. Inline
        # bytes from an unrecognized source are merely unattributed, and until
        # the server can mint an attachment there is no supported way to make
        # one recognized, so blocking by default would break working
        # deployments on a patch upgrade. Warn now, block once minting ships.
        self.file_data_parts = file_data_parts
        self.unminted_file_parts = unminted_file_parts
        self.attachment_hash_max_bytes = attachment_hash_max_bytes
        self._attachment_scanners: dict[str, AttachmentScanner] = {}
        self._warned_attachments: set[tuple[str, str]] = set()
        self._artifact_notice_emitted = False
        self._generated_invocation_ids: weakref.WeakKeyDictionary[object, str] = (
            weakref.WeakKeyDictionary()
        )
        self._generated_invocation_ids_by_context_id: dict[int, str] = {}
        self._generated_context_ids_by_invocation_id: dict[str, int] = {}
        self._request_text_by_call_key: dict[tuple[str, str], str] = {}
        self._request_object_ids_by_call_key: dict[tuple[str, str], int] = {}
        self._current_llm_call_ids: dict[str, list[str]] = {}
        self._stored_llm_call_ids: dict[int, str] = {}
        self._known_steps: dict[tuple[str, str], StepSchemaDict] = {}
        self._synced_step_keys: set[tuple[str, str]] = set()
        self._step_sync_tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._nudges = NudgeChannel(normalized_name)
        # Claimed nudges live here for the few statements between the halt
        # check and the injection pass, so one claim answers both questions.
        # Keyed by invocation and dropped as it is read; the channel's own
        # ledger is what bounds anything longer-lived.
        self._pending_nudges: dict[str, list[dict[str, Any]]] = {}
        # Applies the server-managed system prompt and model. Holds per-request
        # and per-agent-object state, cleared in ``close()``. ``wrap_managed_prompt``
        # exists for anyone who finds the fence tags leaking into output; the
        # HEAD/TAIL rule works either way, because it never parses delimiters
        # back out of the string.
        self._managed_config = ManagedConfigApplier(
            wrap_managed_prompt_block=wrap_managed_prompt
        )

    def bind(self, agent: Any) -> None:
        """Pre-register known ADK steps before the runner starts."""

        self._warn_on_artifact_service(agent, getattr(agent, "runner", None))
        self._warn_on_undeliverable_boundaries()
        steps = self._discover_steps(agent)
        self._remember_steps(steps)
        self._sync_steps_blocking(steps, raise_on_error=True)

    def _warn_on_undeliverable_boundaries(self) -> None:
        """Say which boundaries an operator stop will not be able to land at.

        A deployment that disables a hook silently narrows where a human can
        stop this agent, and the narrowing is invisible from the console: the
        stop button still appears, the halt is still recorded, and it simply
        never lands. Without ``before_tool`` a stop cannot prevent a tool from
        running, which is the difference between stopping before the email is
        sent and stopping after.
        """

        missing = [
            boundary
            for boundary in ("before_model", "before_tool")
            if boundary not in self.enabled_hooks
        ]
        if not missing:
            return
        logger.warning(
            "Agent Control operator stops cannot land at these boundaries "
            "because their hooks are disabled: %s. Guidance and stops are "
            "delivered only at enabled boundaries.",
            ", ".join(missing),
        )

    async def close(self) -> None:
        """Release per-run state and cancel any outstanding step-sync tasks."""

        pending_tasks = list(self._step_sync_tasks.values())
        for task in pending_tasks:
            task.cancel()
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)

        self._step_sync_tasks.clear()
        self._generated_invocation_ids.clear()
        self._generated_invocation_ids_by_context_id.clear()
        self._generated_context_ids_by_invocation_id.clear()
        self._request_text_by_call_key.clear()
        self._request_object_ids_by_call_key.clear()
        self._current_llm_call_ids.clear()
        self._stored_llm_call_ids.clear()
        self._attachment_scanners.clear()
        self._warned_attachments.clear()
        self._pending_nudges.clear()
        self._managed_config.clear()
        await self._nudges.aclose()
        self._artifact_notice_emitted = False

        base_close = getattr(super(), "close", None)
        if callable(base_close):
            result = base_close()
            if inspect.isawaitable(result):
                await result

    async def before_model_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_request: LlmRequest,
    ) -> LlmResponse | None:
        """Evaluate controls before an ADK model call."""

        if "before_model" not in self.enabled_hooks:
            return None

        # Server-managed configuration is settled first, before
        # ``extract_request_payload`` runs, so every control below sees the
        # request exactly as it will be sent. The prompt rule runs before the
        # model rule; a test asserts that ordering, because a reordering
        # refactor would otherwise produce a request whose self-reported model
        # names one vendor while the client serves another.
        self._apply_managed_config(callback_context, llm_request)

        scan_invocation_id = self._resolve_invocation_id(callback_context)

        # Operator actions run first, before any bookkeeping and before the
        # control round trip. A halt is a person pressing stop, not a guardrail
        # verdict: it never goes through ``_handle_llm_exception``, so no
        # ``on_violation_callback`` fires with a fabricated deny and the
        # message is not pushed through ``blocked_message_template``. Running
        # first also means no pending-call state exists yet to unwind.
        halt_response = await self._halt_at_model_boundary(
            callback_context, scan_invocation_id
        )
        if halt_response is not None:
            return halt_response

        step_name = self._resolve_llm_step_name(callback_context)
        payload = extract_request_payload(
            llm_request,
            scanner=self._attachment_scanner(scan_invocation_id, callback_context),
            placeholder_text=self.attachment_placeholder_text,
        )
        request_text = payload.text

        # Structural refusals run before the engine round trip and before any
        # pending-call bookkeeping, and they run on *every* model call rather
        # than once per invocation. A file that was allowed on call 1 and must
        # be refused on call 3 has to be refused on call 3: post-tool calls are
        # exactly where an injected instruction takes effect.
        refusal = self._refuse_unevaluatable_parts(payload, invocation_id=scan_invocation_id)
        if refusal is not None:
            return refusal

        # Guidance is appended *after* the request has been read, and its text
        # is concatenated onto what the controls below evaluate.
        #
        # The order is the security property. ``extract_request_payload`` takes
        # its text from ``contents[-1]`` and nowhere else, and a nudge is
        # appended as a new trailing ``Content``. Inject first and the real
        # input of that call - the user's message, or the tool result a prompt
        # injection arrived in - is pushed out of the only Content the
        # extractor reads, so the model call carrying a nudge is the one call
        # whose actual content no control ever sees. Queuing a nudge is
        # AUTHENTICATED and authoring a control is ADMIN, so that ordering
        # would hand the cheaper credential a one-call bypass of the more
        # expensive one. Concatenating instead means both are evaluated.
        injected = await self._inject_claimed_nudges(
            callback_context, llm_request, scan_invocation_id
        )
        if injected:
            request_text = "\n\n".join([part for part in (request_text, *injected) if part])

        invocation_id: str | None = None
        call_id: str | None = None
        if "after_model" in self.enabled_hooks:
            invocation_id = scan_invocation_id
            call_id = self._register_llm_request(invocation_id, llm_request, request_text)
        self._ensure_step_known(
            self._build_llm_step_schema(step_name, callback_context=callback_context),
        )

        context = self._safe_context(
            step_type="llm",
            stage="pre",
            agent_control_block=payload.context_block(),
            callback_context=callback_context,
            llm_request=llm_request,
        )

        try:
            await _evaluate_and_enforce(
                self.agent_name,
                step_name,
                input=request_text,
                context=context,
                step_type="llm",
                stage="pre",
            )
        except Exception as exc:
            response = self._handle_llm_exception(
                exc,
                callback_context=callback_context,
                llm_request=llm_request,
                step_name=step_name,
                stage="pre",
            )
            if response is not None and invocation_id is not None and call_id is not None:
                self._clear_pending_llm_state(invocation_id, call_id, llm_request=llm_request)
            return response
        return None

    async def after_model_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_response: LlmResponse,
    ) -> LlmResponse | None:
        """Evaluate controls after an ADK model call."""

        if "after_model" not in self.enabled_hooks:
            return None

        step_name = self._resolve_llm_step_name(callback_context)
        invocation_id = self._resolve_invocation_id(callback_context)
        call_id = self._resolve_llm_call_id(llm_response, invocation_id)
        input_text = self._request_text_by_call_key.pop((invocation_id, call_id), "")
        payload = extract_response_payload(
            llm_response,
            scanner=self._attachment_scanner(invocation_id, callback_context),
            placeholder_text=self.attachment_placeholder_text,
        )
        self._clear_pending_llm_state(invocation_id, call_id, llm_response=llm_response)
        output_text = payload.text
        self._ensure_step_known(
            self._build_llm_step_schema(step_name, callback_context=callback_context),
        )

        context = self._safe_context(
            step_type="llm",
            stage="post",
            agent_control_block=payload.context_block(),
            callback_context=callback_context,
            llm_response=llm_response,
        )

        try:
            await _evaluate_and_enforce(
                self.agent_name,
                step_name,
                input=input_text,
                output=output_text,
                context=context,
                step_type="llm",
                stage="post",
            )
        except Exception as exc:
            return self._handle_llm_exception(
                exc,
                callback_context=callback_context,
                llm_response=llm_response,
                step_name=step_name,
                stage="post",
            )
        return None

    async def on_model_error_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_request: LlmRequest,
        error: Exception,
    ) -> LlmResponse | None:
        """Clean up request correlation when ADK reports a model error."""

        _ = error
        invocation_id = self._resolve_invocation_id(callback_context)
        call_id = self._resolve_llm_call_id(llm_request, invocation_id)
        self._clear_pending_llm_state(invocation_id, call_id, llm_request=llm_request)
        return None

    async def before_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
    ) -> dict[str, Any] | None:
        """Evaluate controls before an ADK tool call."""

        if "before_tool" not in self.enabled_hooks:
            return None

        # Same reasoning as the model boundary, and this one is why a stop
        # means what a person means by it: with no check here, a stop pressed
        # while the model was deciding lets the tool run and blocks only the
        # model call after it, by which time the side effect has happened.
        halt_result = await self._halt_at_tool_boundary(tool, tool_context)
        if halt_result is not None:
            return halt_result

        step_name = self._resolve_tool_step_name(tool, tool_context=tool_context)
        self._ensure_step_known(self._build_tool_step_schema(tool, step_name))
        context = self._safe_context(
            step_type="tool",
            stage="pre",
            tool=tool,
            tool_args=tool_args,
            tool_context=tool_context,
        )

        try:
            await _evaluate_and_enforce(
                self.agent_name,
                step_name,
                input=tool_args,
                context=context,
                step_type="tool",
                stage="pre",
            )
        except Exception as exc:
            return self._handle_tool_exception(
                exc,
                tool=tool,
                tool_args=tool_args,
                tool_context=tool_context,
                step_name=step_name,
                stage="pre",
            )
        return None

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        result: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Evaluate controls after an ADK tool call."""

        if "after_tool" not in self.enabled_hooks:
            return None

        # ADK still fires this callback for a tool whose body never ran, and
        # hands it the substitute dict as the tool's ``result``. Evaluating
        # that would fire a post-tool control on work that did not happen -
        # reporting a violation for a side effect nobody had, and letting a
        # control's own denial replace the operator's stop message on the way
        # back to the model. Once an invocation is latched, every tool in it is
        # blocked at the boundary above, so the latch is the exact predicate.
        if self._nudges.latched_halt(self._resolve_invocation_id(tool_context)):
            return None

        step_name = self._resolve_tool_step_name(tool, tool_context=tool_context)
        self._ensure_step_known(self._build_tool_step_schema(tool, step_name))
        context = self._safe_context(
            step_type="tool",
            stage="post",
            tool=tool,
            tool_args=tool_args,
            tool_context=tool_context,
            result=result,
        )

        try:
            await _evaluate_and_enforce(
                self.agent_name,
                step_name,
                input=tool_args,
                output=result,
                context=context,
                step_type="tool",
                stage="post",
            )
        except Exception as exc:
            return self._handle_tool_exception(
                exc,
                tool=tool,
                tool_args=tool_args,
                tool_context=tool_context,
                step_name=step_name,
                stage="post",
            )
        return None

    # -- operator actions: stops and guidance ----------------------------

    async def _halt_at_model_boundary(
        self,
        callback_context: CallbackContext,
        invocation_id: str,
    ) -> LlmResponse | None:
        """Block this model call when an operator has stopped this turn.

        Two ways a stop arrives here. A halt claimed at a *tool* boundary is
        latched against this invocation and fires with no second network call.
        Anything else comes back on the nudge claim, which runs at this
        boundary anyway.

        Returning an ``LlmResponse`` ends the invocation outright and costs no
        model call, which is the behaviour the halt design rests on and the one
        the spike measured against a real ``adk api_server``.
        """

        latched = self._nudges.latched_halt(invocation_id)
        if latched is not None:
            # The latch is per invocation and is dropped the moment it fires,
            # so a stop cannot outlive the turn it was bound to.
            self._nudges.release_invocation(invocation_id)
            return build_blocked_llm_response(latched.model_message())

        identity = self._session_identity(callback_context)
        if identity is None:
            return None

        halt, claimed = await self._nudges.claim_at_model_boundary(
            identity=identity, invocation_id=invocation_id
        )
        if claimed:
            # Held for the injection pass immediately after this one, so a
            # single claim serves both decisions.
            self._pending_nudges[invocation_id] = claimed
        if halt is None:
            return None

        self._nudges.release_invocation(invocation_id)
        self._pending_nudges.pop(invocation_id, None)
        return build_blocked_llm_response(halt.model_message())

    async def _inject_claimed_nudges(
        self,
        callback_context: CallbackContext,
        llm_request: LlmRequest,
        invocation_id: str,
    ) -> list[str]:
        """Append the operator's exact words to this request, as a user turn.

        Every claimed body is evaluated by the control engine first, and a
        denied one is reported back as ``rejected`` naming the control rather
        than disappearing. Whatever is neither injected nor rejected goes back
        on the queue untouched: no counter moves for a nudge nobody attempted,
        which is what keeps "queued" from silently becoming "undelivered".

        Returns the text that actually landed in the request, so the caller can
        fold it into what the deployment's own controls evaluate. The caller
        cannot recover it from the request afterwards without displacing the
        real content, which is the whole reason this returns anything.
        """

        claimed = self._pending_nudges.pop(invocation_id, None)
        if not claimed:
            return []

        identity = self._session_identity(callback_context)
        if identity is None:
            return []

        # Reported like any other step, and for the same reason: a step this
        # server has never seen cannot be bound to a control in the console, so
        # without this the dedicated evaluation below would run against a step
        # nobody could attach anything to.
        self._ensure_step_known(
            self._build_llm_step_schema(nudge_step_name(self.agent_name)),
        )

        approved, rejected = await self._nudges.evaluate_and_partition(
            nudges=claimed, invocation_id=invocation_id
        )

        acks: list[dict[str, Any]] = [
            {
                "id": nudge["id"],
                "outcome": "rejected",
                "rejected_by_control": nudge.get("rejected_by_control"),
            }
            for nudge in rejected
        ]

        injected: list[str] = []
        for nudge in approved:
            text = build_nudge_text(nudge["body"])
            if append_operator_turn(llm_request, text):
                injected.append(text)
                self._nudges.record_injection(invocation_id, nudge["id"])
                acks.append(
                    {
                        "id": nudge["id"],
                        "outcome": "applied",
                        "trace_id": identity.trace_id,
                    }
                )
            else:
                # An attempted injection that did not land. This is the only
                # outcome that counts against a nudge's life.
                acks.append({"id": nudge["id"], "outcome": "failed"})

        handled = {ack["id"] for ack in acks}
        acks.extend(
            {"id": nudge["id"], "outcome": "released"}
            for nudge in claimed
            if nudge["id"] not in handled
        )
        await self._nudges.acknowledge(identity=identity, acks=acks)
        return injected

    async def _halt_at_tool_boundary(
        self,
        tool: BaseTool,
        tool_context: ToolContext,
    ) -> dict[str, Any] | None:
        """Stop this tool from running when an operator has stopped the turn.

        Returning a dict here prevents the tool body executing - proven by the
        absence of the side effect, not inferred from a transcript. The
        invocation would ordinarily carry on and make one more model call, so
        two things happen: the stop is latched against this invocation so that
        call blocks with no further network round trip, and the invocation is
        asked to end here, which removes the round trip entirely where the
        executor honours it.
        """

        invocation_id = self._resolve_invocation_id(tool_context)
        latched = self._nudges.latched_halt(invocation_id)
        if latched is not None:
            return build_blocked_tool_response(latched.tool_message())

        identity = self._session_identity(tool_context)
        if identity is None:
            return None

        decision = await self._nudges.claim_at_tool_boundary(
            identity=identity,
            invocation_id=invocation_id,
            tool_name=resolve_tool_name(tool),
        )
        if decision is None:
            return None

        self._nudges.latch_halt(invocation_id, decision)
        self._request_end_of_invocation(tool_context)
        return build_blocked_tool_response(decision.tool_message())

    def _request_end_of_invocation(self, tool_context: ToolContext) -> None:
        """Ask ADK to stop after this blocked tool result.

        ``skip_summarization`` is the one action flag observed to end an
        invocation from a tool boundary; ``escalate`` and ``end_of_agent``
        propagate onto the event and the agent calls the model anyway. Best
        effort by design: the latch above is what makes the stop correct, and
        this only decides whether stopping costs one more model call.
        """

        try:
            actions = getattr(tool_context, "actions", None)
            if actions is not None:
                actions.skip_summarization = True
        except Exception:
            logger.debug(
                "Could not request end of invocation after a halt", exc_info=True
            )

    def _session_identity(self, context: Any) -> SessionIdentity | None:
        """Read this session's identity and write credential out of ADK state.

        Seeded by Agent Control at session creation and refreshed with every
        turn. When it is absent - an executor session this control plane did
        not create, or a deployment with no runtime auth - no claim is made at
        all. Agent-scoped delivery is not a fallback: it would inject one
        person's typed sentence into a stranger's conversation, and the halt
        equivalent would stop a turn nobody asked to stop.
        """

        return SessionIdentity.read(getattr(context, "state", None))

    def _resolve_llm_step_name(self, callback_context: CallbackContext) -> str:
        raw_name = resolve_agent_name(callback_context)
        return self._resolve_step_name(
            raw_name,
            step_type="llm",
            callback_context=callback_context,
        )

    def _resolve_tool_step_name(
        self,
        tool: BaseTool,
        *,
        tool_context: ToolContext | None = None,
        agent_step_name: str | None = None,
    ) -> str:
        raw_name = resolve_tool_name(tool)
        resolved_agent_step_name = agent_step_name
        if resolved_agent_step_name is None:
            resolved_agent_step_name = self._resolve_tool_agent_step_name(tool_context)
        default_name = (
            f"{resolved_agent_step_name}.{raw_name}" if resolved_agent_step_name else raw_name
        )
        return self._resolve_step_name(
            default_name,
            step_type="tool",
            override_keys=(raw_name,),
            tool=tool,
            tool_context=tool_context,
            raw_name=raw_name,
            agent_step_name=resolved_agent_step_name,
        )

    def _resolve_step_name(
        self,
        default_name: str,
        *,
        step_type: Literal["llm", "tool"],
        override_keys: Iterable[str] = (),
        **kwargs: Any,
    ) -> str:
        candidate_names = [default_name, *override_keys]
        for candidate_name in candidate_names:
            override = self.step_name_overrides.get(candidate_name)
            if override:
                return override

        if self.step_name_resolver is not None:
            resolved = self.step_name_resolver(
                step_type=step_type,
                default_name=default_name,
                override_keys=tuple(candidate_names),
                **kwargs,
            )
            if isinstance(resolved, str) and resolved:
                return resolved

        return default_name

    def _resolve_tool_agent_step_name(self, tool_context: ToolContext | None) -> str | None:
        if tool_context is None:
            return None

        callback_context = getattr(tool_context, "callback_context", None)
        if callback_context is not None:
            return self._resolve_llm_step_name(callback_context)

        raw_agent_name = resolve_tool_agent_name(tool_context)
        if raw_agent_name is None:
            return None

        return self._resolve_step_name(
            raw_agent_name,
            step_type="llm",
            agent_name=raw_agent_name,
            tool_context=tool_context,
        )

    def _safe_context(
        self,
        *,
        step_type: Literal["llm", "tool"],
        stage: Literal["pre", "post"],
        agent_control_block: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any] | None:
        """Build ``Step.context``: the deployment's extractor, then ours on top.

        The ``agent_control`` key is reserved and server-authored. An extractor
        that supplies one has it dropped, because the audited party must not
        author its own audit record. The merge happens outside the extractor's
        ``try`` so a failing extractor cannot take the attachment block down
        with it, and the block is emitted even when there are no attachments so
        that ``attachment_summary.count`` is a selectable zero rather than a
        missing path a threshold control would read as absent.
        """

        extracted: dict[str, Any] | None = None
        if self.context_extractor is not None:
            try:
                extracted = self.context_extractor(step_type=step_type, stage=stage, **kwargs)
            except Exception:
                logger.warning("Google ADK context_extractor failed", exc_info=True)

        reported = self._reported_config_context()

        if agent_control_block is None and not reported:
            return extracted

        merged: dict[str, Any] = dict(extracted) if isinstance(extracted, dict) else {}
        merged.pop("agent_control", None)
        if agent_control_block is not None:
            merged["agent_control"] = agent_control_block
        # Applied after the extractor's output so a deployment extractor cannot
        # overwrite what this process reports about its own configuration.
        merged.update(reported)
        return merged

    @staticmethod
    def _reported_config_context() -> dict[str, Any]:
        """Which prompt version and which model this process believes it is on.

        Stamped onto every control execution event so an incident can answer
        "which configuration produced this decision" instead of joining an event
        timestamp against a version's ``created_at`` - a join whose error is
        unbounded, not one refresh interval, because a process whose refreshes
        keep failing runs a superseded prompt for hours with nothing recording
        it.

        Everything here is under ``reported.`` and is an **unverified
        self-report**. The server stamps its own view under ``agent_control.``
        on ingest, and a divergence between the two is the queryable signal that
        an agent was running a stale or forged configuration. ``model_id`` is
        the key an operator will actually reach for, and it is also the one they
        will over-trust, which is why it is only meaningful next to the server's.
        """

        snapshot = agent_control.get_agent_config()
        if snapshot is None:
            return {}
        return {
            "reported.config_etag": snapshot.etag,
            "reported.prompt_source": snapshot.prompt_source,
            "reported.model_source": snapshot.model_source,
            "reported.model_id": snapshot.model_id,
        }

    def _warn_on_artifact_service(self, *holders: Any) -> None:
        """Say plainly what an artifact service means for coverage.

        ``save_artifact`` is ADK-internal: it goes through none of the four
        plugin callbacks and the pinned surface exposes no hook, so an agent can
        write bytes no control ever sees. What is covered is the moment those
        bytes are loaded into a model request, where the walk describes them,
        the manifest does not match, and ``source`` reads ``unknown``.

        ``bind()`` alone cannot answer this. In ADK the artifact service is a
        ``Runner`` constructor argument, not an agent field, and ``bind()`` is
        handed the root agent, so a bind-time check on the agent finds nothing
        even when a service is configured. The first model call settles it via
        the invocation context, which is where the service is actually reachable.
        Emitted at most once per plugin.
        """

        if self._artifact_notice_emitted:
            return
        for holder in holders:
            if holder is None:
                continue
            if getattr(holder, "artifact_service", None) is None:
                continue
            self._artifact_notice_emitted = True
            logger.info(
                "This ADK app has an artifact service configured. Agent Control "
                "cannot evaluate save_artifact writes; artifacts are described "
                "only when they are loaded into a model request, where they "
                "report source='unknown'."
            )
            return

    def _artifact_service_holder(self, callback_context: CallbackContext) -> Any:
        """Reach the invocation context without ever failing a model call."""

        try:
            getter = getattr(callback_context, "get_invocation_context", None)
            if callable(getter):
                return getter()
        except Exception:
            logger.debug("Could not read the ADK invocation context", exc_info=True)
        return None

    def _attachment_scanner(
        self,
        invocation_id: str,
        callback_context: CallbackContext,
    ) -> AttachmentScanner:
        """Return the invocation's scanner, with its manifest refreshed.

        One scanner per invocation is what makes hash memoization and
        ``first_seen`` work: within a turn a carried-over file is hashed once
        and reported as carried over on every call after the first.
        """

        if not self._artifact_notice_emitted:
            self._warn_on_artifact_service(self._artifact_service_holder(callback_context))

        scanner = self._attachment_scanners.get(invocation_id)
        if scanner is None:
            scanner = AttachmentScanner(hash_max_bytes=self.attachment_hash_max_bytes)
            self._attachment_scanners[invocation_id] = scanner
            while len(self._attachment_scanners) > _MAX_TRACKED_INVOCATIONS:
                self._attachment_scanners.pop(next(iter(self._attachment_scanners)), None)

        scanner.manifest = self._read_attachment_manifest(callback_context)
        return scanner

    def _read_attachment_manifest(
        self,
        callback_context: CallbackContext,
    ) -> dict[str, Any] | None:
        """Read the per-turn ``{sha256: attachment_key}`` manifest from session state.

        Absent, unreadable or stale all return ``None``, which leaves every
        descriptor at ``source="unknown"`` and ``unminted_count == count``. A
        deployment that has not built the server side therefore gets a loud,
        correct refusal from the ``unminted_count`` control rather than a quiet
        pass, and a broken state channel degrades the same way.
        """

        state = getattr(callback_context, "state", None)
        if state is None:
            return None

        block = _state_lookup(state, _STATE_BLOCK_KEY)
        if isinstance(block, dict):
            manifest = block.get(_STATE_MANIFEST_KEY)
            if isinstance(manifest, dict):
                return manifest

        manifest = _state_lookup(state, _STATE_MANIFEST_DOTTED_KEY)
        if isinstance(manifest, dict):
            return manifest
        return None

    def _refuse_unevaluatable_parts(
        self,
        payload: ExtractedPayload,
        *,
        invocation_id: str,
    ) -> LlmResponse | None:
        """Refuse file parts no control in this deployment could ever evaluate.

        This is an SDK-level structural refusal, not a guardrail verdict, so it
        does not run through ``_handle_llm_exception``: no
        ``on_violation_callback`` fires and ``blocked_message_template`` is not
        applied. Those exist to describe a control's decision, and no control
        made this one.
        """

        file_data_parts = [d for d in payload.attachments if d.is_file_data]
        if file_data_parts and self.file_data_parts == "block":
            for descriptor in file_data_parts:
                logger.warning(
                    "Agent Control blocked a file_data part: its bytes are fetched "
                    "by the model provider and cannot be evaluated. %s",
                    descriptor.log_summary(),
                )
            return build_blocked_llm_response(_FILE_DATA_BLOCKED_MESSAGE)

        unminted = [
            d for d in payload.attachments if not d.is_file_data and d.source != "operator"
        ]
        if not unminted or self.unminted_file_parts == "allow":
            return None

        for descriptor in unminted:
            self._warn_once(invocation_id, descriptor)

        if self.unminted_file_parts == "block":
            return build_blocked_llm_response(_UNMINTED_BLOCKED_MESSAGE)
        return None

    def _warn_once(self, invocation_id: str, descriptor: AttachmentDescriptor) -> None:
        """Log an unattributed file part once per invocation.

        Deduplication is on logging only. Enforcement above runs on every model
        call, because a carried-over file is read by the model on every call.
        """

        identity = descriptor.sha256 or f"{descriptor.content_index}:{descriptor.part_index}"
        key = (invocation_id, identity)
        if key in self._warned_attachments:
            return
        if len(self._warned_attachments) >= _MAX_WARNED_ATTACHMENTS:
            self._warned_attachments.clear()
        self._warned_attachments.add(key)
        logger.warning(
            "Agent Control saw a file part this control plane did not issue; "
            "no control has seen its contents. %s",
            descriptor.log_summary(),
        )

    def _format_message(self, reason: str) -> str:
        if not self.blocked_message_template:
            return reason
        try:
            return self.blocked_message_template.format(reason=reason)
        except Exception:
            logger.warning("Invalid blocked_message_template; using raw reason", exc_info=True)
            return reason

    def _handle_llm_exception(
        self,
        exc: Exception,
        *,
        callback_context: CallbackContext,
        step_name: str,
        stage: Literal["pre", "post"],
        llm_request: LlmRequest | None = None,
        llm_response: LlmResponse | None = None,
    ) -> LlmResponse | None:
        self._invoke_callback(step_name, "llm", stage, exc)

        if (
            isinstance(exc, agent_control.ControlSteerError)
            and stage == "pre"
            and llm_request is not None
        ):
            if self._inject_steering_guidance(
                llm_request, exc.steering_context, source="control"
            ):
                return None

        if isinstance(exc, agent_control.ControlSteerError):
            message = exc.steering_context or exc.message
        elif isinstance(exc, agent_control.ControlViolationError):
            message = exc.message
        else:
            logger.error("Google ADK model control evaluation failed", exc_info=True)
            message = f"Agent Control could not evaluate the request safely: {exc}"

        return build_blocked_llm_response(self._format_message(message))

    def _handle_tool_exception(
        self,
        exc: Exception,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        step_name: str,
        stage: Literal["pre", "post"],
    ) -> dict[str, Any]:
        self._invoke_callback(step_name, "tool", stage, exc)

        if isinstance(exc, agent_control.ControlSteerError):
            message = exc.steering_context or exc.message
        elif isinstance(exc, agent_control.ControlViolationError):
            message = exc.message
        else:
            logger.error("Google ADK tool control evaluation failed", exc_info=True)
            message = f"Agent Control could not evaluate the request safely: {exc}"

        return build_blocked_tool_response(self._format_message(message))

    def _invoke_callback(
        self,
        step_name: str,
        step_type: Literal["llm", "tool"],
        stage: Literal["pre", "post"],
        exc: Exception,
    ) -> None:
        if self.on_violation_callback is None:
            return
        if not isinstance(
            exc,
            (agent_control.ControlViolationError, agent_control.ControlSteerError),
        ):
            return

        result_like = {
            "action": "steer" if isinstance(exc, agent_control.ControlSteerError) else "deny",
            "message": exc.message,
            "steering_context": (
                exc.steering_context if isinstance(exc, agent_control.ControlSteerError) else None
            ),
        }
        try:
            self.on_violation_callback(
                {
                    "agent": self.agent_name,
                    "step_name": step_name,
                    "step_type": step_type,
                    "stage": stage,
                },
                result_like,
            )
        except Exception:
            logger.warning("Google ADK on_violation_callback failed", exc_info=True)

    def _apply_managed_config(
        self,
        callback_context: CallbackContext,
        llm_request: LlmRequest,
    ) -> None:
        """Apply the cached managed system prompt and model to this request.

        Reads the snapshot the SDK's refresh loop published; never fetches.
        ``prompt_source`` and ``model_source`` were resolved server-side, so
        every reason not to apply - unmanaged, cleared, disabled, the model gone
        from the allowlist, delivery gated off on a credential-less server - has
        already collapsed into them and is not re-derived here.

        Nothing in this method may raise into a model call. A configuration
        feature that can take an agent down is worse than one that does nothing.
        """

        snapshot = agent_control.get_agent_config()
        if snapshot is None:
            return

        try:
            self._managed_config.apply_prompt(llm_request, snapshot.managed_prompt)
        except Exception:
            logger.debug("Applying the managed system prompt failed", exc_info=True)

        try:
            agent = self._resolve_callback_agent(callback_context)
            self._managed_config.apply_model(
                llm_request,
                agent,
                snapshot.managed_model,
                fetched_at=snapshot.fetched_at,
                max_staleness_seconds=state.model_max_staleness_seconds,
            )
        except Exception:
            logger.debug("Applying the managed model failed", exc_info=True)

    @staticmethod
    def _resolve_callback_agent(callback_context: CallbackContext) -> Any | None:
        """The agent object the flow will read when it resolves the model.

        ``CallbackContext.get_invocation_context()`` is public and returns a
        ``model_copy`` of the invocation context. The copy is shallow, so
        ``.agent`` on it is the same object the flow reads - no reaching into
        privates. Falls back to ``callback_context.agent`` for the hand-written
        fakes and for older surfaces that do not expose the method.
        """

        getter = getattr(callback_context, "get_invocation_context", None)
        if callable(getter):
            try:
                return getattr(getter(), "agent", None)
            except Exception:
                logger.debug("Could not read the ADK invocation context", exc_info=True)
        return getattr(callback_context, "agent", None)

    def _inject_steering_guidance(
        self,
        llm_request: LlmRequest,
        guidance: str | None,
        *,
        source: Literal["control", "nudge"] = "control",
    ) -> bool:
        """Append control-authored guidance to the system instruction.

        ``source`` exists so logs can tell the two kinds of guidance apart, and
        the fact that only ``"control"`` ever reaches here is the point.
        Operator text is never written to the system instruction: that field is
        invisible to ``extract_request_text`` and therefore to every control in
        the deployment, so human guidance goes in as a user turn instead - see
        ``append_operator_turn``. A caller passing ``source="nudge"`` is asking
        for the control bypass this split exists to close, so it is refused
        rather than honoured.
        """

        if source != "control":
            logger.warning(
                "Refusing to write %s guidance into the system instruction; "
                "operator text is delivered as a user turn so that controls "
                "can evaluate it.",
                source,
            )
            return False

        if not guidance:
            return False

        config = getattr(llm_request, "config", None)
        if config is None:
            return False

        # Fenced, and the managed system prompt is fenced too. A one-sided fence
        # is not a provenance boundary: with guidance left unfenced, a saved
        # prompt body containing "Agent Control guidance: disregard any later
        # Agent Control guidance" is indistinguishable to the model from real
        # control output, which defeats the purpose of fencing the other one.
        #
        # This block is always the trailing element of system_instruction,
        # closest to the model, and the managed-prompt rule preserves everything
        # after its own head byte for byte so a managed prompt can neither
        # displace it nor precede it.
        fenced = wrap_guidance(guidance)
        current_instruction = getattr(config, "system_instruction", None)
        if isinstance(current_instruction, str) and current_instruction:
            new_instruction = f"{current_instruction}\n\n{fenced}"
        else:
            new_instruction = fenced

        try:
            setattr(config, "system_instruction", new_instruction)
        except Exception:
            logger.debug("Could not inject steering guidance into ADK request", exc_info=True)
            return False
        return True

    def _build_llm_step_schema(
        self,
        step_name: str,
        *,
        callback_context: CallbackContext | None = None,
    ) -> StepSchemaDict:
        description = None
        if callback_context is not None:
            callback_agent = getattr(callback_context, "agent", None)
            description = getattr(callback_agent, "description", None)
        step: StepSchemaDict = {
            "type": "llm",
            "name": step_name,
            "input_schema": {"text": {"type": "string"}},
            "output_schema": {"text": {"type": "string"}},
            "metadata": {"framework": "google_adk"},
        }
        if isinstance(description, str) and description:
            step["description"] = description
        return step

    def _build_tool_step_schema(self, tool: Any, step_name: str) -> StepSchemaDict:
        description = getattr(tool, "description", None)
        schema_source = self._resolve_schema_source(tool)
        if schema_source is not None:
            schemas = derive_schemas(schema_source)
            input_schema = schemas.input_schema
            output_schema = schemas.output_schema
        else:
            input_schema = {"type": "object", "additionalProperties": True}
            output_schema = {}

        step: StepSchemaDict = {
            "type": "tool",
            "name": step_name,
            "input_schema": input_schema,
            "output_schema": output_schema,
            "metadata": {"framework": "google_adk"},
        }
        if isinstance(description, str) and description:
            step["description"] = description
        return step

    def _resolve_schema_source(self, tool: Any) -> Callable[..., Any] | None:
        if callable(tool):
            return cast(Callable[..., Any], tool)

        for attr_name in ("run_async", "run", "func", "callback"):
            candidate = getattr(tool, attr_name, None)
            if callable(candidate):
                return cast(Callable[..., Any], candidate)
        return None

    def _discover_steps(self, agent: Any) -> list[StepSchemaDict]:
        steps: list[StepSchemaDict] = []
        for current_agent in self._iter_agents(agent):
            resolved_agent_name: str | None = None
            agent_name = getattr(current_agent, "name", None)
            if isinstance(agent_name, str) and agent_name:
                resolved_agent_name = self._resolve_step_name(
                    agent_name,
                    step_type="llm",
                    callback_context=None,
                    agent=current_agent,
                )
                steps.append(
                    self._build_llm_step_schema(resolved_agent_name),
                )

            for tool in self._iter_tools(current_agent):
                tool_name = self._resolve_tool_step_name(
                    tool,
                    agent_step_name=resolved_agent_name,
                )
                steps.append(self._build_tool_step_schema(tool, tool_name))

        deduped: dict[tuple[str, str], StepSchemaDict] = {}
        for step in steps:
            deduped[(step["type"], step["name"])] = step
        return list(deduped.values())

    def _iter_agents(self, root_agent: Any) -> Iterable[Any]:
        seen: set[int] = set()
        stack = [root_agent]
        while stack:
            current = stack.pop()
            identity = id(current)
            if identity in seen:
                continue
            seen.add(identity)
            yield current

            # ADK composite agents are not guaranteed to expose one uniform
            # child-agent attribute, so traverse a small set of observed names.
            # Extend this list if future ADK agent containers expose a different
            # stable child collection attribute.
            for attr_name in ("sub_agents", "agents", "children"):
                nested = getattr(current, attr_name, None)
                if isinstance(nested, dict):
                    stack.extend(nested.values())
                elif isinstance(nested, (list, tuple, set)):
                    stack.extend(nested)
                elif nested is not None and not isinstance(nested, (str, bytes)):
                    stack.append(nested)

    def _iter_tools(self, agent: Any) -> Iterable[Any]:
        tools = getattr(agent, "tools", None)
        if isinstance(tools, (list, tuple, set)):
            return tools
        return []

    def _remember_steps(self, steps: Iterable[StepSchemaDict]) -> None:
        for step in steps:
            key = (step["type"], step["name"])
            self._known_steps[key] = step

    def _ensure_step_known(self, step: StepSchemaDict) -> None:
        key = (step["type"], step["name"])
        self._known_steps[key] = step
        if key in self._synced_step_keys or key in self._step_sync_tasks:
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._sync_steps_blocking([step], raise_on_error=False)
            return

        self._schedule_step_sync(loop, step)

    def _schedule_step_sync(
        self,
        loop: asyncio.AbstractEventLoop,
        step: StepSchemaDict,
    ) -> None:
        key = (step["type"], step["name"])
        task = loop.create_task(self._sync_steps_async([step]))
        self._step_sync_tasks[key] = task

        def _callback(completed_task: asyncio.Task[None]) -> None:
            self._on_step_sync_done(key, completed_task)

        task.add_done_callback(_callback)

    def _on_step_sync_done(
        self,
        step_key: tuple[str, str],
        task: asyncio.Task[None],
    ) -> None:
        self._step_sync_tasks.pop(step_key, None)
        if task.cancelled():
            return

        error = task.exception()
        if error is None:
            return

        logger.warning("Failed to sync Google ADK steps to Agent Control", exc_info=error)

    def _context_key(self, callback_context: CallbackContext) -> object:
        """Build a stable correlation key across model before/after hooks.

        Prefer the callback object itself when it is hashable so identity reuse
        cannot alias two different callback objects. Fall back to ``id()`` only
        for unhashable callback types, relying on ADK to keep the callback alive
        across the paired before/after callbacks.
        """
        try:
            hash(callback_context)
        except TypeError:
            return id(callback_context)
        return callback_context

    def _resolve_invocation_id(self, callback_context: CallbackContext) -> str:
        """Resolve the ADK invocation ID used to correlate model lifecycle hooks."""

        invocation_id = getattr(callback_context, "invocation_id", None)
        if invocation_id is not None:
            return str(invocation_id)

        try:
            cached = self._generated_invocation_ids.get(callback_context)
        except TypeError:
            cached = self._generated_invocation_ids_by_context_id.get(id(callback_context))
        if cached is not None:
            return cached

        generated = str(uuid4())
        try:
            self._generated_invocation_ids[callback_context] = generated
        except TypeError:
            context_id = id(callback_context)
            self._generated_invocation_ids_by_context_id[context_id] = generated
            self._generated_context_ids_by_invocation_id[generated] = context_id
        return generated

    def _resolve_llm_call_id(self, obj: Any, invocation_id: str | None = None) -> str:
        """Resolve a model call ID across before/after/error callbacks."""

        stored = self._stored_llm_call_ids.get(id(obj))
        if stored:
            return stored

        request_id = getattr(obj, "request_id", None)
        if request_id:
            return str(request_id)

        if invocation_id is not None:
            stack = self._current_llm_call_ids.get(invocation_id)
            if stack:
                return stack[-1]

        logger.debug("Google ADK LLM correlation fell back to object identity")
        return f"llm_{id(obj)}"

    def _register_llm_request(
        self,
        invocation_id: str,
        llm_request: LlmRequest,
        request_text: str,
    ) -> str:
        call_id = self._resolve_llm_call_id(llm_request, invocation_id)
        call_key = (invocation_id, call_id)
        self._stored_llm_call_ids[id(llm_request)] = call_id
        self._request_text_by_call_key[call_key] = request_text
        self._request_object_ids_by_call_key[call_key] = id(llm_request)
        self._current_llm_call_ids.setdefault(invocation_id, []).append(call_id)
        return call_id

    def _clear_pending_llm_state(
        self,
        invocation_id: str,
        call_id: str,
        *,
        llm_request: LlmRequest | None = None,
        llm_response: LlmResponse | None = None,
    ) -> None:
        call_key = (invocation_id, call_id)
        self._request_text_by_call_key.pop(call_key, None)

        request_object_id = self._request_object_ids_by_call_key.pop(call_key, None)
        if request_object_id is not None:
            self._stored_llm_call_ids.pop(request_object_id, None)

        if llm_request is not None:
            self._stored_llm_call_ids.pop(id(llm_request), None)
            # The prompt rule's per-request baseline dies with the request, so
            # a long-lived process does not accumulate one entry per model call.
            self._managed_config.forget_request(llm_request)

        if llm_response is not None:
            self._stored_llm_call_ids.pop(id(llm_response), None)

        self._clear_current_llm_call_id(invocation_id, call_id=call_id)

    def _clear_current_llm_call_id(
        self,
        invocation_id: str,
        *,
        call_id: str | None = None,
    ) -> None:
        stack = self._current_llm_call_ids.get(invocation_id)
        if not stack:
            return

        if call_id is None:
            stack.pop()
        else:
            for index in range(len(stack) - 1, -1, -1):
                if stack[index] == call_id:
                    del stack[index]
                    break

        if not stack:
            context_id = self._generated_context_ids_by_invocation_id.pop(invocation_id, None)
            if context_id is not None:
                self._generated_invocation_ids_by_context_id.pop(context_id, None)
            self._current_llm_call_ids.pop(invocation_id, None)
            # The scanner is deliberately NOT dropped here. This branch runs
            # when the pending-call stack empties, which is after every model
            # call, not at the end of the invocation. Dropping it would reset
            # the hash memo and first_seen on every call, so a carried-over file
            # would report itself new forever and carried_over_count would be
            # stuck at zero. Scanners are bounded and evicted oldest-first
            # instead; ADK exposes no end-of-invocation hook to do better.

    def _sync_steps_blocking(
        self,
        steps: Iterable[StepSchemaDict],
        *,
        raise_on_error: bool,
    ) -> None:
        pending_steps = [
            step
            for step in steps
            if (step["type"], step["name"]) not in self._synced_step_keys
        ]
        if not pending_steps:
            return

        if raise_on_error:
            self._run_sync(self._sync_steps_async(pending_steps))
            return

        try:
            self._run_sync(self._sync_steps_async(pending_steps))
        except Exception:
            logger.warning("Failed to sync Google ADK steps to Agent Control", exc_info=True)

    async def _sync_steps_async(self, steps: list[StepSchemaDict]) -> None:
        current = state.current_agent
        if current is None or state.server_url is None:
            return
        if current.agent_name != self.agent_name:
            raise RuntimeError(
                "Google ADK step binding requires agent_control.init() to be called "
                "with the same agent_name as AgentControlPlugin."
            )

        async with AgentControlClient(
            base_url=state.server_url,
            api_key=state.api_key,
            api_key_header=state.api_key_header,
        ) as client:
            response = await agents.get_agent(client, self.agent_name)
            existing = GetAgentResponse.model_validate(response)
            existing_keys = {(step.type, step.name) for step in existing.steps}
            pending_steps = [
                step for step in steps if (step["type"], step["name"]) not in existing_keys
            ]
            if not pending_steps:
                self._synced_step_keys.update((step["type"], step["name"]) for step in steps)
                return

            register_response = await agents.register_agent(
                client,
                current,
                steps=[dict(step) for step in pending_steps],
                conflict_mode="strict",
            )
            controls = register_response.get("controls")
            if isinstance(controls, list):
                state.server_controls = list(controls)
            self._synced_step_keys.update((step["type"], step["name"]) for step in steps)

    def _run_sync(self, coro: Any) -> Any:
        """Run an async registration helper from sync setup paths."""

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)

        result_box: dict[str, Any] = {}

        def _thread_worker() -> None:
            try:
                result_box["value"] = asyncio.run(coro)
            except Exception as exc:  # noqa: BLE001
                result_box["error"] = exc

        thread = threading.Thread(target=_thread_worker, daemon=True)
        thread.start()
        thread.join(timeout=_SYNC_TIMEOUT_SECONDS)

        if thread.is_alive():
            raise RuntimeError(
                "Timed out while syncing Google ADK steps to Agent Control."
            )

        if "error" in result_box:
            raise result_box["error"]
        return result_box.get("value")
