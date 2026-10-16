"""The agent loop: user -> LLM -> MCP tools -> LLM ... -> answer. No Qt, no Binary Ninja imports."""
import json

from . import llm
from .mcp_client import McpClient, McpError

STUB_PREFIX = "[output pruned"


class Cancelled(Exception):
    pass


# ------------------------------------------------------------------ context management
def cap_result(text, limit):
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated %s more chars; page with offset/limit or narrow the query]" % format(
        len(text) - limit, ","
    )


def _call_index(messages):
    idx = {}
    for m in messages:
        for tc in m.get("tool_calls") or []:
            idx[tc["id"]] = (tc["function"]["name"], tc["function"].get("arguments", ""))
    return idx


def compact_history(messages, high, low, keep_recent=4):
    """Replace the oldest tool outputs by one-line stubs once they exceed `high` chars in total.

    Pruning works in batches (down to `low`) rather than one message per step, so the prompt
    prefix stays stable between prunes and provider-side prompt caching keeps working.
    Mutates the message dicts in place. Returns the number of messages pruned.
    """
    tool_msgs = [m for m in messages if m.get("role") == "tool" and not (m.get("content") or "").startswith(STUB_PREFIX)]
    total = sum(len(m.get("content") or "") for m in tool_msgs)
    if total <= high:
        return 0
    calls = _call_index(messages)
    protected = {id(m) for m in tool_msgs[-max(1, keep_recent):]}
    pruned = 0
    for m in tool_msgs:
        if total <= low:
            break
        if id(m) in protected:
            continue
        name, args = calls.get(m.get("tool_call_id"), ("tool", ""))
        old = len(m.get("content") or "")
        stub = "%s: %s(%s) returned %s chars; call it again if you still need it]" % (
            STUB_PREFIX, name, args[:120], format(old, ","))
        m["content"] = stub
        total -= old - len(stub)
        pruned += 1
    return pruned


def repair_history(messages):
    """Make sure every assistant tool_call has a tool reply (e.g. after a crash mid-run)."""
    out = []
    for i, m in enumerate(messages):
        out.append(m)
        if m.get("role") == "assistant" and m.get("tool_calls"):
            answered = set()
            for n in messages[i + 1:]:
                if n.get("role") != "tool":
                    break
                answered.add(n.get("tool_call_id"))
            for tc in m["tool_calls"]:
                if tc["id"] not in answered:
                    out.append({"role": "tool", "tool_call_id": tc["id"], "content": "Error: interrupted before this tool ran."})
    messages[:] = out


def build_request(messages, system_prompt, echo_reasoning=False):
    """Only send the fields the OpenAI format knows about."""
    out = [{"role": "system", "content": system_prompt}]
    for m in messages:
        r = {"role": m["role"], "content": m.get("content") or ""}
        if m.get("tool_calls"):
            r["tool_calls"] = m["tool_calls"]
        if m["role"] == "tool":
            r["tool_call_id"] = m["tool_call_id"]
        if echo_reasoning and m.get("reasoning_content"):
            r["reasoning_content"] = m["reasoning_content"]
        out.append(r)
    return out


def mcp_to_openai_tools(tools):
    out = []
    for t in tools:
        schema = t.get("inputSchema") or {"type": "object", "properties": {}}
        schema.setdefault("type", "object")
        out.append({"type": "function", "function": {"name": t["name"], "description": t.get("description", ""), "parameters": schema}})
    return out


# ------------------------------------------------------------------ the loop
def run_agent(messages, cfg, profile, emit, cancel):
    """messages: list whose dicts are shared with the stored chat (mutated in place by pruning).
    emit(kind, payload): kind in {"message", "status"}; every new message goes through emit.
    """
    repair_history(messages)
    mcp = McpClient(cfg["mcp_url"], cfg.get("mcp_token", ""))
    try:
        emit("status", "Connecting to Binary Ninja...")
        mcp.initialize()
        tools = mcp_to_openai_tools(mcp.list_tools())
        known = {t["function"]["name"] for t in tools}

        def push(msg):
            messages.append(msg)
            emit("message", msg)

        high = int(cfg["context_tool_chars"])
        for step in range(1, int(cfg["max_steps"]) + 1):
            if cancel.is_set():
                raise Cancelled()
            compact_history(messages, high, int(high * 0.6))
            emit("status", "Thinking (step %d)..." % step)
            reply, usage = llm.chat_completion(
                profile, build_request(messages, cfg["system_prompt"], profile.get("echo_reasoning")), tools
            )
            push(reply)
            if usage and usage.get("prompt_tokens"):
                emit("status", "Step %d - %s prompt tokens" % (step, format(usage["prompt_tokens"], ",")))
            calls = reply.get("tool_calls") or []
            if not calls:
                return
            fatal = None
            for call in calls:
                name = call["function"]["name"]
                if cancel.is_set():
                    text = "Cancelled by the user."
                elif fatal:
                    text = "Skipped because the MCP connection failed."
                else:
                    emit("status", "Running %s..." % name)
                    try:
                        args = json.loads(call["function"].get("arguments") or "{}")
                        if not isinstance(args, dict):
                            raise ValueError("arguments must be a JSON object")
                    except ValueError as e:
                        args, text = None, "Error: invalid JSON arguments (%s)." % e
                    if args is not None:
                        if name not in known:
                            text = "Error: unknown tool '%s'." % name
                        else:
                            try:
                                text, is_err = mcp.call_tool(name, args)
                                if is_err:
                                    text = "Tool error: " + text
                            except McpError as e:
                                fatal, text = e, "Error: %s" % e
                push({"role": "tool", "tool_call_id": call["id"], "content": cap_result(text, int(cfg["max_result_chars"]))})
            if fatal:
                raise fatal
        push({"role": "assistant", "content": "_Stopped after %d steps. Say \"continue\" to keep going._" % cfg["max_steps"]})
    finally:
        mcp.close()
