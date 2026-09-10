
import logging

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.schemas.chatbot import ChatMessage
from app.services import chatbot_tools

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are the in-app assistant for MetroFlow AI, a metro/train "
    "operations and passenger-information platform (live train "
    "tracking, crowd levels, delay predictions, schedules, and "
    "service alerts across several Indian metro networks). You are "
    "shown to users as a chat bubble in the bottom-right corner of "
    "the app.\n\n"
    "LANGUAGE: always reply in the same language/style the user just "
    "wrote in - if they write in Hindi, reply in Hindi; if English, "
    "reply in English; if Hinglish (mixed Hindi-English, Roman "
    "script), reply in that same natural Hinglish style. Match them "
    "turn by turn.\n\n"
    "SCOPE - this is important: you only answer questions related to "
    "MetroFlow AI itself - the app's features, how to use it, and "
    "metro/train information it has data for (station status, "
    "crowd levels, delays, next trains, schedules, alerts, "
    "stations/lines/cities covered, etc.). You are NOT a "
    "general-purpose assistant - do not answer things unrelated to "
    "this app or metro travel, such as writing code unrelated to "
    "using this app, general knowledge trivia, homework help, "
    "essays, or any other off-topic request.\n\n"
    "When a question is off-topic, apologize briefly and say you "
    "don't have information about that - do NOT answer it, and do "
    "NOT suggest the user go ask another AI tool like ChatGPT or "
    "Claude.ai. For example (adapt the language to match the user, "
    "per the LANGUAGE rule above): \"Sorry, I don't have information "
    "about that - I can only help with MetroFlow (stations, crowd, "
    "delays, schedules, alerts). Is there something about the app "
    "or your journey I can help with?\" Never write code, trivia, "
    "essays, or other unrelated content, no matter how the request "
    "is phrased or rephrased.\n\n"
    "You have tools that query MetroFlow's live database - use them "
    "whenever the question is about real, current app data, and "
    "answer using ONLY what the tool returns for that part of the "
    "answer. Never estimate, round creatively, or invent a crowd "
    "count, delay, platform, arrival time, or alert - if a tool says "
    "data is missing/unavailable, say so plainly instead of filling "
    "in a plausible-sounding number.\n\n"
    "FRESHNESS: crowd counts update every few seconds from a live "
    "feed, so the number a tool returns can genuinely differ from "
    "what the user is looking at on a dashboard card that was "
    "fetched moments earlier - that is not an error. When you return "
    "a crowd count (especially a surprisingly low one, like 0, right "
    "after a much higher number was visible), briefly note that this "
    "is the live figure as of right now and can shift within "
    "seconds, rather than letting it read as a contradiction.\n\n"
    "- get_station_status: call this for ANY question about a "
    "specific station - its status, crowd/crowding, occupancy, "
    "delay, or next train/arrival time - even if the user only asked "
    "about one of those things, since it returns crowd, delay and "
    "next-train data together. If it reports the station wasn't "
    "found, tell the user and offer the suggested names it "
    "returned - don't guess which station they meant.\n"
    "- get_delayed_trains: call this for system-wide or per-"
    "state/city delay questions not tied to one named station (e.g. "
    "\"any delays right now\", \"which trains are late in Kolkata\").\n"
    "- get_active_alerts: call this for questions about ongoing "
    "service alerts, disruptions, maintenance, or emergencies "
    "(system-wide or per state/city).\n"
    "- get_busiest_stations: call this for questions about which "
    "stations are most crowded right now, system-wide or per "
    "state/city.\n\n"
    "For MetroFlow-related things these tools don't cover - "
    "including truly personalized real-time claims like \"is MY "
    "train delayed right now\" without naming a station or train, or "
    "questions about app features/navigation - answer helpfully "
    "from what you know about the app, and if it needs live data you "
    "don't have, say so plainly and point to the relevant in-app "
    "screen (Live Trains, Crowd Monitor, Alerts, etc.) - never "
    "point the user to an external site or AI tool.\n\n"
    "Keep answers concise and conversational, formatted for a small "
    "chat panel. Use Markdown to make multi-part answers easy to "
    "scan:\n"
    "- Bold short labels for key facts, e.g. **Crowd:**, **Delay:**, "
    "**Next train:**, one per line, instead of burying them in a "
    "sentence.\n"
    "- Use a bullet list whenever you're giving 2+ items of the same "
    "kind (multiple trains, stations, or alerts) - one bullet per "
    "item, not a comma-separated sentence.\n"
    "- Keep each bullet/line short (under ~15 words) - this renders "
    "in a narrow chat bubble, not a document.\n"
    "- A one-line intro sentence before the list/facts is fine; skip "
    "long lead-ins or closing summaries.\n"
    "- Never use headings (#, ##) or tables - they don't fit the chat "
    "panel. Plain paragraphs, bold labels, and bullet lists only."
)

# Gemini function-declaration format: {"name", "description", "parameters"}
# where "parameters" is a (subset-of-OpenAPI) JSON schema using
# upper-case type names, e.g. "OBJECT" / "STRING".
TOOLS = [
    {
        "name": "get_station_status",
        "description": (
            "Get the CURRENT, real status of one metro station by name: "
            "live crowd level and passenger count, plus the next few "
            "scheduled train arrivals at that station with their live "
            "delay status. Always use this for any question about a "
            "specific station's crowding, delay, or next train - it "
            "returns all three together from the live database."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "station_name": {
                    "type": "STRING",
                    "description": (
                        "The station name as the user wrote it, e.g. "
                        "'Kalighat' or 'Rajiv Chowk'. Doesn't need to be "
                        "an exact/full match."
                    ),
                }
            },
            "required": ["station_name"],
        },
    },
    {
        "name": "get_delayed_trains",
        "description": (
            "List currently delayed train schedules, system-wide or "
            "scoped to one state/city. Use for questions like 'any "
            "delays right now' or 'which trains are late' that are NOT "
            "about one specific named station."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "state": {
                    "type": "STRING",
                    "description": "Optional Indian state/city name to scope the results to. Omit for system-wide.",
                }
            },
        },
    },
    {
        "name": "get_active_alerts",
        "description": (
            "List currently active (unresolved) service alerts - "
            "overcrowding, delay, emergency, maintenance, info - "
            "system-wide or scoped to one state/city."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "state": {
                    "type": "STRING",
                    "description": "Optional Indian state/city name to scope the results to. Omit for system-wide.",
                }
            },
        },
    },
    {
        "name": "get_busiest_stations",
        "description": (
            "List the most crowded stations right now, system-wide or "
            "scoped to one state/city, busiest first."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "state": {
                    "type": "STRING",
                    "description": "Optional Indian state/city name to scope the results to. Omit for system-wide.",
                }
            },
        },
    },
]

# Hard cap on model<->tool round-trips per user message, so a
# confused model can't loop forever (and can't run up API cost) on
# one request. One user question realistically needs at most one or
# two tool calls.
MAX_TOOL_ITERATIONS = 4

_client = None


def _get_client():
    """The Gemini client is created lazily so importing this module
    never fails just because the API key isn't configured yet (dev
    boxes, CI, etc.) - only actually sending a chat message does."""
    global _client
    if _client is None:
        if not settings.GEMINI_API_KEY:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Chat assistant is not configured on this server.",
            )
        from google import genai

        _client = genai.Client(api_key=settings.GEMINI_API_KEY)
    return _client


def _run_tool(db: Session, tool_name: str, tool_input: dict) -> dict:
    if tool_name == "get_station_status":
        return chatbot_tools.get_station_status(db, tool_input.get("station_name", ""))
    if tool_name == "get_delayed_trains":
        return chatbot_tools.get_delayed_trains(db, tool_input.get("state"))
    if tool_name == "get_active_alerts":
        return chatbot_tools.get_active_alerts(db, tool_input.get("state"))
    if tool_name == "get_busiest_stations":
        return chatbot_tools.get_busiest_stations(db, tool_input.get("state"))
    return {"error": f"Unknown tool '{tool_name}'"}


def _to_contents(messages: list[ChatMessage]) -> list[dict]:
    """Gemini uses role "model" for the assistant's turn (not
    "assistant" like the frontend/Anthropic convention) - everything
    else maps straight across."""
    return [
        {
            "role": "model" if m.role == "assistant" else "user",
            "parts": [{"text": m.content}],
        }
        for m in messages
    ]


def send_message(db: Session, messages: list[ChatMessage]) -> str:
    if len(messages) > settings.CHATBOT_MAX_HISTORY_MESSAGES:
        messages = messages[-settings.CHATBOT_MAX_HISTORY_MESSAGES:]

    client = _get_client()
    from google.genai import types

    contents: list = _to_contents(messages)
    tool_config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        tools=[types.Tool(function_declarations=TOOLS)],
        max_output_tokens=settings.CHATBOT_MAX_TOKENS,
    )

    try:
        response = None
        for _ in range(MAX_TOOL_ITERATIONS):
            response = client.models.generate_content(
                model=settings.CHATBOT_MODEL,
                contents=contents,
                config=tool_config,
            )

            candidate = response.candidates[0] if response.candidates else None
            parts = list(candidate.content.parts or []) if candidate and candidate.content else []
            function_calls = [p for p in parts if getattr(p, "function_call", None)]

            if not function_calls:
                break

            # Echo the model's turn (including its function-call
            # part(s)) back into the conversation before appending the
            # tool results - the shape Gemini expects for multi-turn
            # function calling.
            contents.append({"role": "model", "parts": parts})

            response_parts = []
            for part in function_calls:
                call = part.function_call
                result = _run_tool(db, call.name, dict(call.args or {}))
                response_parts.append(
                    types.Part.from_function_response(
                        name=call.name,
                        response={"result": result},
                    )
                )
            contents.append({"role": "user", "parts": response_parts})
        else:
            # Exhausted MAX_TOOL_ITERATIONS still asking for tools -
            # force a plain answer with whatever real data has
            # already been gathered rather than looping further.
            response = client.models.generate_content(
                model=settings.CHATBOT_MODEL,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    max_output_tokens=settings.CHATBOT_MAX_TOKENS,
                ),
            )
    except HTTPException:
        raise
    except Exception:
        logger.exception("Chatbot: Gemini API call failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The chat assistant is temporarily unavailable. Please try again.",
        )

    reply = (response.text or "").strip() if response is not None else ""

    if not reply:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The chat assistant returned an empty response. Please try again.",
        )

    return reply