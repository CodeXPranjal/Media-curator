import os
import re
import json
import logging
import sys

from google.adk.workflow import Workflow, node, RetryConfig
from google.adk.agents import LlmAgent
from google.adk.tools import AgentTool
from google.adk.events import Event
from google.adk.events.request_input import RequestInput
from google.adk.agents.context import Context
from google.adk.apps import App, ResumabilityConfig
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from mcp import StdioServerParameters
from google.genai import types
from .config import config

logger = logging.getLogger("security_audit")
logging.basicConfig(level=logging.INFO)

mcp_server_path = os.path.join(os.path.dirname(__file__), "mcp_server.py")
mcp_toolset = McpToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command=sys.executable,
            args=[mcp_server_path],
        )
    )
)

media_curator = LlmAgent(
    name="media_curator",
    model=config.model,
    instruction="""You are a media curator. The user is asking you for a recommendation in their input.
You MUST generate exactly 1 recommendation matching their requested format (either exactly 1 book or exactly 1 movie/TV show).
Do NOT recommend books if they asked for a Movie/show. Do NOT recommend movies if they asked for a Book.
For the recommendation, provide: title, author/platform, and a short reason why it matches their mood.
Do not ask the user any questions, and do not ask them for their mood or time again. Proceed directly to making exactly 1 recommendation using your tools (search_goodreads, search_tmdb, check_streaming_availability).

Format the output EXACTLY like this (and include fun, relevant emojis in every response):
Here is your [movie/book] recommendation! 🍿📚

**Title:** [title] 🎬📖
**Platform/Author:** [platform/author] 🌐✍️
**Reason:** [reason] ✨💡

ENJOY! 🎉🥳""",
    tools=[mcp_toolset]
)

@node
async def hitl_input_node(ctx: Context, node_input: str):
    if "get_preferences" not in ctx.resume_inputs:
        yield RequestInput(interrupt_id="get_preferences", message="Welcome! 🍿 What is your current mood and how much time do you have available? ⏰")
        return
    preferences = ctx.resume_inputs["get_preferences"]
    ctx.state["user_preferences"] = preferences
    yield Event(output=preferences)

@node(rerun_on_resume=True)
async def hitl_choice_node(ctx: Context, node_input: str):
    loop_count = ctx.state.get("loop_count", 0)
    choice_id = f"choice_{loop_count}"
    
    if choice_id not in ctx.resume_inputs:
        yield RequestInput(interrupt_id=choice_id, message="Okey, movie or book? 🎬📚")
        return
    choice = ctx.resume_inputs[choice_id]
    ctx.state["media_choice"] = choice
    preferences = ctx.resume_inputs.get("get_preferences", "")
    # Combine preferences and choice into a natural language request (stored in state)
    ctx.state["combined_input"] = f"Please recommend exactly 1 {choice.lower()} for me. My mood and available time preferences are: {preferences}."
    # Pass empty output to keep it silent in the UI log
    yield Event(output="")

@node
def security_checkpoint(ctx: Context, node_input: str):
    # Read the combined input from the state (background)
    combined_input = ctx.state.get("combined_input", node_input)
    
    # 1. PII Scrubbing
    scrubbed_input = re.sub(r'[\w\.-]+@[\w\.-]+', '[REDACTED_EMAIL]', combined_input)
    
    audit_log = {"event": "security_check", "severity": "INFO", "action": "pass", "reason": "OK"}
    
    # 2. Prompt Injection Check
    injection_keywords = ["ignore previous instructions", "bypass", "system prompt"]
    if any(kw in scrubbed_input.lower() for kw in injection_keywords):
        audit_log.update({"severity": "CRITICAL", "action": "block", "reason": "prompt_injection"})
        logger.warning(json.dumps(audit_log))
        return Event(output="Request blocked due to security policies. 🛡️", route="SECURITY_EVENT")
        
    # 3. Domain-specific rule (No adult content)
    adult_keywords = ["nsfw", "porn", "adult only"]
    if any(kw in scrubbed_input.lower() for kw in adult_keywords):
        audit_log.update({"severity": "WARNING", "action": "block", "reason": "adult_content_blocked"})
        logger.warning(json.dumps(audit_log))
        return Event(output="I cannot recommend that type of content. 🛑", route="SECURITY_EVENT")
        
    logger.info(json.dumps(audit_log))
    ctx.state["scrubbed_query"] = scrubbed_input
    # Pass empty output to keep it silent in the UI log
    return Event(output="", route="SAFE")

@node(rerun_on_resume=True)
async def run_media_curator(ctx: Context, node_input: str):
    query = ctx.state.get("scrubbed_query", node_input)
    
    # Inject recommendation history into query to avoid duplicates
    history = ctx.state.get("recommendation_history", [])
    if history:
        history_str = ", ".join(history)
        query = f"{query}\n\nCRITICAL: Do NOT recommend any of the following items that you have already recommended in this chat session: {history_str}."
    
    import asyncio
    last_error = None
    for attempt in range(5):
        try:
            result = await ctx.run_node(media_curator, node_input=query)
            
            # Safely extract text from result (types.Content or string)
            rec_text = ""
            if hasattr(result, "parts") and result.parts:
                rec_text = "".join(part.text for part in result.parts if getattr(part, "text", None))
            else:
                rec_text = str(result)
                
            # Parse recommended title to add to history
            title_match = re.search(r"\*\*Title:\*\*\s*(.+?)(?:\s*(?:🎬|📖)|$)", rec_text)
            if title_match:
                title = title_match.group(1).strip()
            else:
                title = rec_text.split("\n")[0].strip()
                
            if "recommendation_history" not in ctx.state:
                ctx.state["recommendation_history"] = []
            ctx.state["recommendation_history"].append(title)
            
            # Yield content formatted correctly as a types.Content object
            # Save to state and yield empty output to keep it silent in the UI log
            ctx.state["rec_text"] = rec_text
            yield Event(output="")
            return
        except Exception as e:
            last_error = e
            # Try to parse wait time from error message, e.g. "Please retry in 19s"
            wait_time = 5.0
            msg = str(e)
            match = re.search(r'Please retry in ([0-9\.]+)s', msg, re.IGNORECASE)
            if match:
                wait_time = float(match.group(1)) + 1.5  # add 1.5s buffer
            else:
                wait_time = 3.0 * (attempt + 1)
                
            logger.warning(f"Attempt {attempt + 1} failed with error: {e}. Retrying in {wait_time:.1f}s...")
            await asyncio.sleep(wait_time)
            
    # If all attempts fail, raise the final exception
    if last_error:
        raise last_error

@node
def finalize_output(ctx: Context, node_input: str | None = None):
    rec_text = ctx.state.get("rec_text", "")
    ctx.state["final_recommendation"] = rec_text
    yield Event(output=rec_text)

@node(rerun_on_resume=True)
async def loop_prompt_node(ctx: Context, node_input: str):
    loop_count = ctx.state.get("loop_count", 0)
    another_id = f"another_{loop_count}"

    rec_text = ctx.state.get("rec_text", "")

    if another_id not in ctx.resume_inputs:
        yield RequestInput(
            interrupt_id=another_id, 
            message=(
                "Want another recommendation? 🤔\n"
                "👉 Type 'yes' (or 'ok') to choose movie/book again! 🎬📚\n"
                "👉 Type 'no' to exit. 👋"
            )
        )
        return
    
    response = ctx.resume_inputs[another_id].lower()
    if "yes" in response or "ok" in response:
        ctx.state["loop_count"] = loop_count + 1
        yield Event(output="", route="LOOP")
    else:
        msg = "Okey byee then! 👋 And remember, if you ever feel bored and need a recommendation , just come here and start a new session! 🎬📚🍿✨"
        yield Event(
            message=types.Content(
                role="model",
                parts=[types.Part.from_text(text=msg)]
            ),
            route="END"
        )

root_agent = Workflow(
    name="media_curator_workflow",
    edges=[
        ('START', hitl_input_node),
        (hitl_input_node, hitl_choice_node),
        (hitl_choice_node, security_checkpoint),
        (security_checkpoint, {
            "SAFE": run_media_curator,
            "SECURITY_EVENT": finalize_output
        }),
        (run_media_curator, loop_prompt_node),
        (loop_prompt_node, {
            "LOOP": hitl_choice_node,
            "END": finalize_output
        })
    ],
    retry_config=RetryConfig(
        max_attempts=5,
        initial_delay=2.0,
        max_delay=30.0,
        backoff_factor=2.0
    )
)

app = App(
    name="app",
    root_agent=root_agent,
    resumability_config=ResumabilityConfig(is_resumable=True)
)
