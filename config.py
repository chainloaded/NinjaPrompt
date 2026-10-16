"""User settings. Global (one file in Binary Ninja's user directory), NOT stored in any .bndb
(API keys!). Always read fresh from disk so every window/tab sees the same settings."""
import json
import os
import shutil

DEFAULT_SYSTEM_PROMPT = """You are an expert reverse engineer working inside Binary Ninja. \
You can inspect the binary the user currently has open through tools.

- Prefer tools over guessing. Look things up, then answer.
- Write addresses as 0x401000 (they become clickable links for the user).
- Old tool output is pruned from your context to save space, so state important findings \
(names, addresses, conclusions) in your replies as you go.
- If a result says it was truncated, page with offset/limit or narrow the query.
- Be concise."""

DEFAULTS = {
    "profiles": [],
    "active_profile": "",
    "mcp_url": "http://127.0.0.1:24642/mcp",
    "mcp_token": "",
    "system_prompt": DEFAULT_SYSTEM_PROMPT,
    "max_steps": 40,
    "max_result_chars": 20000,   # cap for a single tool result
    "context_tool_chars": 120000,  # when all kept tool results exceed this, prune the oldest
}


def new_profile():
    return {
        "name": "New model",
        "base_url": "https://api.openai.com/v1",
        "model": "",
        "api_key": "",
        "api_key_env": "",
        "temperature": "",
        "reasoning_effort": "",
        "echo_reasoning": False,
    }


def _dir():
    import binaryninja

    return binaryninja.user_directory()


def path():
    return os.path.join(_dir(), "ninjaprompt.json")


def _legacy_path():  # settings file of the pre-rename "open_sidekick" version
    return os.path.join(_dir(), "open_sidekick.json")


def load(file=None):
    cfg = json.loads(json.dumps(DEFAULTS))
    for f in [file] if file else [path(), _legacy_path()]:
        try:
            with open(f, "r", encoding="utf-8") as fh:
                cfg.update(json.load(fh))
            break
        except (OSError, ValueError):
            continue
    return cfg


def save(cfg, file=None):
    file = file or path()
    if os.path.exists(file):  # never destroy a file we could not parse
        try:
            with open(file, "r", encoding="utf-8") as fh:
                json.load(fh)
        except ValueError:
            shutil.copy2(file, file + ".bak")
        except OSError:
            pass
    tmp = file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, file)


def update(**changes):
    """Read-modify-write of single keys, so a stale in-memory copy can never overwrite other settings."""
    cfg = load()
    cfg.update(changes)
    save(cfg)
    return cfg


def resolve_key(profile):
    env = (profile.get("api_key_env") or "").strip()
    if env and os.environ.get(env):
        return os.environ[env]
    return profile.get("api_key") or ""
