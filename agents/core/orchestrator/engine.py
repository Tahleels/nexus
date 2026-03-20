"""Core orchestration engine for Hub Agents.

Implements the agentic "plan → call tools → observe → respond" loop that
powers every Hub Agent chat turn:

- ``_is_small_talk`` — fast-path detector that skips tool schemas/prompt
  boilerplate for greetings and chit-chat to save tokens.
- ``ShortTermMemory`` — bounded conversation buffer with auto-summarization.
- ``ContextBuilder`` — builds system prompts and OpenAI tool-call JSON schemas.
- ``Executor`` — runs a single tool by name via the tool registry.
- ``Orchestrator`` — drives the full loop: calls the LLM, executes any
  requested tools, feeds results back, and streams NDJSON status events
  until a final answer is produced or ``max_loops`` is reached.

``blueprints/agents_hub_bp.py`` builds on top of this module via
``HubExecutor``/``HubOrchestrator``, which wrap ``Executor``/``Orchestrator``
to inject hub-specific context (user identity, per-tool config, SQL Server
agent records) without modifying this module.

The LLM call itself (``Orchestrator._call_llm``) goes through
``llm_providers`` (see that package's docstring) rather than talking to
OpenAI directly, so an agent's ``provider``/``model`` fields (defaulting to
"openai"/"gpt-4o") determine which backend actually serves the turn.
"""
import json
import re
import time
from datetime import datetime, date
from decimal import Decimal
from typing import Generator


class _SafeEncoder(json.JSONEncoder):
    """JSON encoder that can serialize Decimal and datetime/date values.

    Used when dumping tool results and NDJSON stream events, since those
    payloads may contain values returned directly from database queries.
    """

    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        return super().default(obj)

# Max query_database rows forwarded to the LLM per tool call (the UI table still
# gets the full set via the "tool_result" SSE event — see the tool-call loop below).
LLM_ROW_LIMIT = 100

# ─── SMALL TALK DETECTION ─────────────────────────────────────────────────────
# Short greetings/chit-chat never need tools, RAG context, or the full tool
# schema list — skipping all of that for these turns is most of the token cost.
_SMALL_TALK_PATTERNS = (
    r"hi+", r"hello+", r"hey+a?", r"yo", r"sup", r"howdy",
    r"good\s*(morning|afternoon|evening|night)",
    r"how\s*(are\s*you|r\s*u|are\s*things|'?s\s*it\s*going|have\s*you\s*been)",
    r"what'?s?\s*up",
    r"thanks?|thank\s*you|thx|ty|cheers",
    r"ok(ay)?|cool|nice|great|awesome|got\s*it|sounds\s*good|perfect|alright",
    r"bye|goodbye|see\s*y?a|see\s*you|later|good\s*night",
    r"lol|haha+|hmm+|test|ping|yes|no|sure",
)
_SMALL_TALK_RE = re.compile(
    r"^\s*(" + "|".join(_SMALL_TALK_PATTERNS) + r")[\s,!.?]*$", re.IGNORECASE)


def _is_small_talk(text: str) -> bool:
    """True for short greetings/chit-chat that need no tools or document context."""
    text = (text or "").strip()
    if not text or len(text) > 40:
        return False
    return bool(_SMALL_TALK_RE.match(text))

# ─── MEMORY SYSTEM ────────────────────────────────────────────────────────────

class ShortTermMemory:
    """Bounded in-memory conversation buffer with auto-summarization.

    Keeps at most ``max_messages`` turns. When the buffer overflows, all but
    the 4 most recent messages are collapsed into a one-line ``summary`` so
    older context is not lost outright but stops growing the prompt forever.
    """

    def __init__(self, max_messages=10):
        """Initialize an empty buffer.

        Args:
            max_messages: Maximum number of messages to keep before older
                ones are summarized away.
        """
        self.messages     = []
        self.max_messages = max_messages
        self.summary      = ""

    def add(self, role: str, content: str):
        """Append a message and summarize older history if over capacity.

        Args:
            role: Message role (e.g. "user" or "assistant").
            content: Message text.
        """
        self.messages.append({"role": role, "content": content,
                               "timestamp": datetime.utcnow().isoformat()})
        if len(self.messages) > self.max_messages:
            self._summarize_old()

    def _summarize_old(self):
        """Collapse all but the last 4 messages into a placeholder summary."""
        old          = self.messages[:-4]
        self.summary = f"[Earlier context: {len(old)} messages]"
        self.messages = self.messages[-4:]

    def get_history(self):
        """Return the buffer as a list of chat messages for the LLM call.

        If a summary exists, it is prepended as a synthetic user/assistant
        exchange so the model sees compressed older context ahead of the
        verbatim recent messages.

        Returns:
            List of ``{"role", "content"}`` dicts in chronological order.
        """
        msgs = []
        if self.summary:
            msgs.append({"role": "user",      "content": f"[Memory]: {self.summary}"})
            msgs.append({"role": "assistant",  "content": "Understood."})
        for m in self.messages:
            msgs.append({"role": m["role"], "content": m["content"]})
        return msgs

    def clear(self):
        """Discard all stored messages and the summary."""
        self.messages = []; self.summary = ""


# ─── CONTEXT BUILDER ─────────────────────────────────────────────────────────

class ContextBuilder:
    """Builds the system prompt and OpenAI tool-call schemas for an agent turn."""

    @staticmethod
    def build_system_prompt(agent: dict) -> str:
        """Build the full system prompt for a normal (non-small-talk) turn.

        Args:
            agent: Agent record dict; uses ``name``, ``objective``, and
                ``system_prompt`` keys.

        Returns:
            The system prompt string, including document-generation and
            tool-usage rules and the current UTC time. Tool names/descriptions
            are intentionally NOT restated here — they're already sent via the
            OpenAI ``tools`` API parameter (see ``build_tool_definitions``).
            Repeating them in plain text would double-bill those tokens on
            every single turn.
        """
        return (
            f"You are {agent.get('name', 'AI Agent')}.\n"
            f"Objective: {agent.get('objective', 'Help users effectively')}\n\n"
            f"{agent.get('system_prompt', '')}\n\n"
            f"Document generation: this platform auto-generates real downloadable PDF, "
            f"PowerPoint, Word, Excel, CSV, Markdown, or dashboard files right after your "
            f"reply. Never tell users to use Word/Google Docs or say you can't make documents — "
            f"when one is requested, just give ONE short confirmation sentence "
            f"(e.g. 'Sure! Creating your PDF now.'); the platform delivers the actual file.\n\n"
            f"Rules: think step by step; use tools only when needed, respond directly when "
            f"sufficient; be concise and accurate; when web_search returns results, use the "
            f"'summary' field directly and cite URLs — never say you can't find information "
            f"if results are present.\n"
            f"Current time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
        )

    @staticmethod
    def build_light_prompt(agent: dict) -> str:
        """Minimal prompt for greetings/small talk. No tool list, no document-capability
        boilerplate — this turn never even gets the 'tools' API parameter."""
        return (
            f"You are {agent.get('name', 'AI Agent')}.\n"
            f"{agent.get('system_prompt', '')}\n\n"
            f"This message is small talk — reply briefly and naturally. No tools needed."
        )

    @staticmethod
    def build_tool_definitions(tool_names: list, registry: dict) -> list:
        """Convert registry tool metadata into OpenAI function-calling schemas.

        Args:
            tool_names: Names of tools enabled for this agent.
            registry: Tool registry dict mapping tool name to its metadata
                (``schema``, ``description``, ``category``).

        Returns:
            List of OpenAI ``{"type": "function", "function": {...}}``
            definitions, one per name found in ``registry`` (unknown names
            are silently skipped).
        """
        definitions = []
        for name in tool_names:
            if name not in registry:
                continue
            meta     = registry[name]
            schema   = meta.get('schema', {})
            props    = {}
            required = []
            for param, info in schema.items():
                props[param] = {
                    "type":        info.get('type', 'string'),
                    "description": info.get('description', param)
                }
                if info.get('required', False):
                    required.append(param)
            definitions.append({
                "type": "function",
                "function": {
                    "name":        name,
                    "description": f"{meta['description']} [{meta['category']}]",
                    "parameters":  {
                        "type":       "object",
                        "properties": props,
                        "required":   required
                    }
                }
            })
        return definitions


# ─── EXECUTOR ────────────────────────────────────────────────────────────────

class Executor:
    """Runs a single named tool via the shared tool registry.

    ``blueprints/agents_hub_bp.py`` defines ``HubExecutor`` as a parallel
    implementation (not a subclass) that additionally injects hub context
    and per-tool config before delegating to ``execute_tool``.
    """

    def __init__(self, api_key: str = ""):
        """Args:
            api_key: API key forwarded to every tool call (e.g. for web_search).
        """
        self.api_key = api_key

    def execute(self, tool_name: str, params: dict) -> dict:
        """Execute one tool call and time it.

        Args:
            tool_name: Name of the tool to run, as registered in TOOL_REGISTRY.
            params: Arguments parsed from the LLM's tool-call request.

        Returns:
            The tool's result dict, with an added ``_execution_time`` (seconds).
        """
        from core.tools.registry import execute_tool
        start  = time.time()
        # Pass api_key into every tool call so web_search (and future tools) can use it
        result = execute_tool(tool_name, {**params, "api_key": self.api_key})
        result['_execution_time'] = round(time.time() - start, 3)
        return result


# ─── MAIN ORCHESTRATOR ───────────────────────────────────────────────────────

class Orchestrator:
    """Drives the agentic tool-calling loop for a single agent turn.

    Owns a ``ShortTermMemory`` (conversation buffer), an ``Executor`` (runs
    tools), and a ``ContextBuilder`` (builds prompts/schemas). ``run()``
    repeatedly calls the LLM, executes any requested tools, and feeds results
    back until the model returns a final answer or ``max_loops`` is reached,
    streaming NDJSON status/tool/final events throughout.
    """

    def __init__(self, api_key: str):
        """Args:
            api_key: OpenAI API key used for both LLM calls and tool execution.
        """
        self.api_key   = api_key
        self.memory    = ShortTermMemory()
        self.executor  = Executor(api_key)   # key forwarded so web_search can use it
        self.ctx       = ContextBuilder()
        self.max_loops = 10

    # ── LLM call (provider-agnostic) ──────────────────────────────
    def _call_llm(self, system: str, messages: list, tools: list = None,
                  model: str = "gpt-4o", provider: str = None) -> dict:
        """Make a single (non-streaming) chat call via ``llm_providers``.

        Args:
            system: System prompt text.
            messages: Chat history including the latest user message.
            tools: OpenAI function-calling tool definitions, or None to omit
                the ``tools``/``tool_choice`` parameters entirely.
            model: Model id, specific to whichever provider is selected.
            provider: ``"openai"`` / ``"bedrock"`` / None (falls back to the
                configured default — see ``llm_providers.factory.get_provider``).

        Returns:
            A dict shaped like OpenAI's chat-completions response body
            (``{"choices": [{"message": {...}, "finish_reason": ...}],
            "usage": {...}}``), or ``{"error": "..."}`` on failure. Kept in
            this shape (rather than returning the provider's own
            ``ChatResult`` dataclass) so the parsing in ``run()`` below stays
            identical regardless of which provider actually served the call.
        """
        from llm_providers.errors import LLMError
        from llm_providers.factory import get_provider

        try:
            result = get_provider(provider).chat(
                system=system, messages=messages, model=model,
                tools=tools or None, max_tokens=4000)
        except LLMError as e:
            return {"error": str(e)}

        return {
            "choices": [{
                "message":       {"content": result.content, "tool_calls": result.tool_calls},
                "finish_reason": result.stop_reason,
            }],
            "usage": result.usage.as_dict(),
        }

    # ── Streaming generator (NDJSON) ─────────────────────────────
    def run(self, user_input: str, agent: dict,
            conversation_history: list = None) -> Generator:
        """Run the agentic tool-calling loop for one user turn, streaming NDJSON.

        Greetings/chit-chat (per ``_is_small_talk``) take a "light" fast path
        that skips tool schemas and prompt boilerplate. Otherwise the loop
        calls the LLM, executes any requested tool calls via ``self.executor``,
        appends results to the message history, and repeats until the model
        returns a final answer or ``self.max_loops`` is exhausted.

        Args:
            user_input: The user's message for this turn.
            agent: Agent record dict (name, objective, system_prompt, model,
                tools, etc).
            conversation_history: Prior turns as ``{"role", "content"}`` dicts;
                only the last 6 are included in the prompt.

        Yields:
            JSON-encoded (NDJSON) strings, each one event of type ``status``,
            ``tool_start``, ``tool_result``, ``final``, or ``error``.
        """
        from core.tools.registry import TOOL_REGISTRY

        model    = agent.get('model', 'gpt-4o')
        provider = agent.get('provider') or None  # None -> llm_providers' configured default

        if _is_small_talk(user_input):
            # Greetings/chit-chat: skip tool schemas and the doc-capability/tool-list
            # boilerplate entirely — this is most of the per-turn token cost.
            system_prompt = self.ctx.build_light_prompt(agent)
            tool_defs     = None
        else:
            agent_tools_raw = agent.get('tools', [])
            # Normalize: extract tool names from both plain strings and {name, config} objects
            agent_tools = [
                t if isinstance(t, str) else t.get('name', '')
                for t in agent_tools_raw if t
            ]
            system_prompt = self.ctx.build_system_prompt(agent)
            tool_defs     = self.ctx.build_tool_definitions(agent_tools, TOOL_REGISTRY)

        messages = []
        if conversation_history:
            for msg in conversation_history[-6:]:
                messages.append({"role": msg['role'], "content": msg['content']})
        messages.append({"role": "user", "content": user_input})

        execution_log = []
        total_tokens  = 0
        input_tokens  = 0
        output_tokens = 0
        loop_count    = 0

        yield json.dumps({"type": "status", "message": "🧠 Orchestrator starting...", "step": "init"}) + "\n"

        while loop_count < self.max_loops:
            loop_count += 1
            yield json.dumps({"type": "status", "message": f"⚡ Planning step {loop_count}...", "step": "planning"}) + "\n"

            response = self._call_llm(system_prompt, messages, tool_defs if tool_defs else None,
                                       model, provider)

            if "error" in response:
                yield json.dumps({"type": "error", "message": response["error"]}) + "\n"
                return

            usage          = response.get("usage", {})
            _in            = usage.get("prompt_tokens", 0)
            _out           = usage.get("completion_tokens", 0)
            input_tokens  += _in
            output_tokens += _out
            total_tokens  += _in + _out

            choice      = response.get("choices", [{}])[0]
            msg_obj     = choice.get("message", {})
            content_txt = msg_obj.get("content") or ""
            tool_calls  = msg_obj.get("tool_calls") or []

            # ── tool calls ────────────────────────────────────────
            if tool_calls:
                messages.append({
                    "role":       "assistant",
                    "content":    content_txt,
                    "tool_calls": tool_calls
                })
                tool_results = []
                for tc in tool_calls:
                    fn_name = tc["function"]["name"]
                    try:
                        fn_args = json.loads(tc["function"].get("arguments", "{}"))
                    except Exception:
                        fn_args = {}
                    tc_id = tc["id"]

                    yield json.dumps({"type": "tool_start", "tool": fn_name,
                                      "input": fn_args, "message": f"🔧 Executing: {fn_name}"}) + "\n"

                    result    = self.executor.execute(fn_name, fn_args)
                    exec_time = result.pop('_execution_time', 0)

                    # Accumulate tokens spent inside tool calls (e.g. sub-agent, data-agent).
                    # These private fields are stripped before the result reaches the LLM.
                    _t_in  = int(result.pop('_input_tokens',  0) or 0)
                    _t_out = int(result.pop('_output_tokens', 0) or 0)
                    _t_tot = int(result.pop('_tokens_used',   0) or 0) or (_t_in + _t_out)
                    input_tokens  += _t_in
                    output_tokens += _t_out
                    total_tokens  += _t_tot

                    # Strip heavy data arrays from execution_log — tool_result already carries them.
                    _log_result = {k: v for k, v in result.items()
                                   if k not in ("data", "columns")} if isinstance(result, dict) else result
                    execution_log.append({"tool": fn_name, "input": fn_args,
                                          "result": _log_result, "execution_time": exec_time,
                                          "timestamp": datetime.utcnow().isoformat()})

                    yield json.dumps({"type": "tool_result", "tool": fn_name, "result": result,
                                      "execution_time": exec_time,
                                      "message": f"✅ {fn_name} done in {exec_time}s"},
                                     cls=_SafeEncoder) + "\n"

                    # The UI table (tool_result above) always gets the full row set so the
                    # user can see/expand everything. The LLM only needs a sample plus the
                    # true count — sending it all N rows burns tokens for no benefit once
                    # results run into the hundreds. The note must not imply the model can
                    # skip rendering rows it did receive — earlier wording ("already shown to
                    # the user") caused the model to under-render (e.g. 10 rows instead of the
                    # 100 it was given) on large result sets; it must still fully format every
                    # row included here.
                    llm_result = result
                    if (fn_name == "query_database" and isinstance(result, dict)
                            and isinstance(result.get("rows"), list)
                            and len(result["rows"]) > LLM_ROW_LIMIT):
                        total_rows = len(result["rows"])
                        llm_result = {**result, "rows": result["rows"][:LLM_ROW_LIMIT]}
                        llm_result["note"] = (
                            f"This tool call matched {total_rows} rows total; only the first "
                            f"{LLM_ROW_LIMIT} are included below to save context. You must still "
                            f"format and display every one of these {LLM_ROW_LIMIT} rows in your "
                            f"table exactly as instructed — do not shorten or sample them further. "
                            f"For the count/summary line, state the true total ({total_rows}), not "
                            f"just the {LLM_ROW_LIMIT} shown."
                        )

                    tool_results.append({"role": "tool", "tool_call_id": tc_id,
                                         "content": json.dumps(llm_result, cls=_SafeEncoder)})

                messages.extend(tool_results)
                continue

            # ── final answer ──────────────────────────────────────
            if content_txt:
                yield json.dumps({"type": "final", "content": content_txt,
                                  "tokens_used":   total_tokens,
                                  "input_tokens":  input_tokens,
                                  "output_tokens": output_tokens,
                                  "execution_log": execution_log,
                                  "loops": loop_count}, cls=_SafeEncoder) + "\n"
                return

            yield json.dumps({"type": "error", "message": "No response from model"},
                             cls=_SafeEncoder) + "\n"
            return

        yield json.dumps({"type": "error", "message": "Max execution loops reached"},
                         cls=_SafeEncoder) + "\n"

    # ── Non-streaming helper ──────────────────────────────────────
    def run_simple(self, user_input: str, agent: dict,
                   conversation_history: list = None) -> dict:
        """Run ``run()`` to completion and collect just the final result.

        Args:
            user_input: The user's message for this turn.
            agent: Agent record dict, same shape as for ``run()``.
            conversation_history: Prior turns, same shape as for ``run()``.

        Returns:
            Dict with ``content`` (final answer text), ``tokens_used``, and
            ``execution_log`` (list of tool-call records). Intermediate
            streaming events are discarded.
        """
        result = {"content": "", "tokens_used": 0, "execution_log": []}
        for chunk in self.run(user_input, agent, conversation_history):
            try:
                data = json.loads(chunk.strip())
                if data.get("type") == "final":
                    result["content"]       = data.get("content", "")
                    result["tokens_used"]   = data.get("tokens_used", 0)
                    result["execution_log"] = data.get("execution_log", [])
            except Exception:
                pass
        return result