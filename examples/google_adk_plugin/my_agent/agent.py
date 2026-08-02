"""Google ADK example using the packaged Agent Control plugin."""

from __future__ import annotations

import os

import agent_control
from agent_control.integrations.google_adk import AgentControlPlugin
from dotenv import load_dotenv
from google.adk.agents import LlmAgent
from google.adk.apps import App

load_dotenv()

# One executor process serves exactly one agent - the SDK holds a single
# current agent and the plugin refuses a mismatch - so this is how you point
# the same example at a different registered agent.
AGENT_NAME = os.getenv("AGENT_CONTROL_AGENT_NAME", "google-adk-plugin")
SERVER_URL = os.getenv("AGENT_CONTROL_URL", "http://localhost:8000")
MODEL_NAME = os.getenv("AGENT_MODEL") or os.getenv("GOOGLE_MODEL", "gemini-2.5-flash")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL")


def _build_model(model_name: str, base_url: str | None) -> object:
    """Return a Gemini model name, or route through LiteLLM for any
    OpenAI-compatible endpoint (a local proxy, Ollama, vLLM, OpenAI itself).

    LiteLLM is imported lazily so the Gemini path keeps working without it.
    """
    if not base_url:
        return model_name

    from google.adk.models.lite_llm import LiteLlm

    if "/" in model_name:
        # LiteLLM routes on the prefix, so "bedrock/..." would reach AWS with the
        # process's ambient credentials and api_base would be ignored outright.
        raise ValueError(
            f"AGENT_MODEL must not contain '/': {model_name!r}. "
            "A provider prefix re-selects the destination host and would send "
            "traffic somewhere other than OPENAI_BASE_URL."
        )

    return LiteLlm(
        model=f"openai/{model_name}",
        api_base=base_url,
        # Pins routing to the endpoint above. Without it a prefixed model name
        # silently chooses its own host.
        custom_llm_provider="openai",
        # A local proxy authenticates upstream itself, so the key is a placeholder
        # unless the endpoint genuinely wants one.
        api_key=os.getenv("OPENAI_API_KEY", "not-used-by-local-proxy"),
    )

CITY_DATA = {
    "new york": {
        "display_name": "New York",
        "local_time": "10:30 AM",
        "weather": "Sunny, 72 F",
    },
    "london": {
        "display_name": "London",
        "local_time": "3:30 PM",
        "weather": "Cloudy, 61 F",
    },
    "tokyo": {
        "display_name": "Tokyo",
        "local_time": "11:30 PM",
        "weather": "Clear, 68 F",
    },
    "testville": {
        "display_name": "Testville",
        "local_time": "9:00 AM",
        "weather": "Mild, 65 F",
    },
}


def _city_record(city: str) -> dict[str, str]:
    """Get deterministic city data for the example tools."""
    return CITY_DATA.get(
        city.lower(),
        {
            "display_name": city.title() or "Unknown City",
            "local_time": "Unknown",
            "weather": "Unavailable",
        },
    )


def _note_for_city(city: str) -> str:
    """Return a deterministic note used by the post-tool demo control."""
    if city.lower() == "testville":
        return "Internal escalation contact: support@internal.example"
    return "Public city information only."


async def get_current_time(city: str) -> dict[str, str]:
    """Get the current time in a city."""
    record = _city_record(city)
    return {
        "city": record["display_name"],
        "value": record["local_time"],
        "note": _note_for_city(city),
    }


async def get_weather(city: str) -> dict[str, str]:
    """Get the weather in a city."""
    record = _city_record(city)
    return {
        "city": record["display_name"],
        "value": record["weather"],
        "note": _note_for_city(city),
    }


agent_control.init(
    agent_name=AGENT_NAME,
    agent_description="Google ADK example using the packaged Agent Control plugin",
    server_url=SERVER_URL,
)


root_agent = LlmAgent(
    name="root_agent",
    model=_build_model(MODEL_NAME, OPENAI_BASE_URL),
    description="City guide agent protected by the packaged Agent Control plugin.",
    instruction=(
        "You are a city guide assistant. Use the available tools for city time or weather. "
        "If a tool returns status=blocked, apologize and explain the message without retrying. "
        "Do not invent internal contacts or unsupported city data."
    ),
    tools=[get_current_time, get_weather],
)

plugin = AgentControlPlugin(agent_name=AGENT_NAME)

try:
    plugin.bind(root_agent)
except Exception as exc:
    raise RuntimeError(
        "Failed to bind Agent Control to the Google ADK app. Start the Agent "
        "Control server and run `uv run python setup_controls.py` before "
        "`uv run adk run my_agent`."
    ) from exc

app = App(name="my_agent", root_agent=root_agent, plugins=[plugin])
