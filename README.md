# NinjaPrompt

A small Binary Ninja sidebar plugin: chat with any OpenAI-format model, which uses Binary Ninja's
built-in MCP server as its tools. No pip dependencies.

## Setup
1. Copy this folder to your plugins directory (Windows: `%APPDATA%\Binary Ninja\plugins\ninjaprompt`) and restart.
2. Binary Ninja settings: enable `ui.mcp.enabled`, restart, then `Plugins > MCP > Start Server`
   (default `http://127.0.0.1:24642/mcp`; set `ui.mcp.token` if you want auth).
3. Open the NinjaPrompt sidebar icon, click the gear, add a model (Base URL, model name, API key or env var),
   use "Test model" and "Test connection".

## How it works
- `mcp_client.py`: minimal streamable-HTTP MCP client (initialize, tools/list, tools/call).
- `llm.py`: `POST {base_url}/chat/completions` with the MCP tools passed as OpenAI `tools`.
- `agent.py`: loop of LLM -> tool calls -> LLM until the model answers. Old tool output is replaced
  by one-line stubs once the kept output exceeds a budget (batch pruning, so prompt caching still works).
- `store.py`: chats live in the BinaryView metadata, so they are saved with the .bndb.
- `ui.py`: sidebar. Colors come from the Qt palette Binary Ninja sets from the active theme.

Settings (including API keys) are stored in `ninjaprompt.json` in Binary Ninja's user directory, never in the .bndb.
