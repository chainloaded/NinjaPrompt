"""OpenAI-compatible /chat/completions client (OpenAI, Ollama, vLLM, DeepSeek, OpenRouter, ...)."""
import json
import time
import urllib.error
import urllib.request

from . import config, net


class LLMError(Exception):
    pass


def _normalize(msg):
    out = {"role": "assistant", "content": msg.get("content") or ""}
    calls = []
    for i, tc in enumerate(msg.get("tool_calls") or []):
        fn = tc.get("function") or {}
        args = fn.get("arguments")
        if not isinstance(args, str):
            args = json.dumps(args or {})
        calls.append(
            {
                "id": tc.get("id") or "call_%d_%d" % (int(time.time() * 1000), i),
                "type": "function",
                "function": {"name": fn.get("name", ""), "arguments": args or "{}"},
            }
        )
    if calls:
        out["tool_calls"] = calls
    if msg.get("reasoning_content"):
        out["reasoning_content"] = msg["reasoning_content"]
    return out


def chat_completion(profile, messages, tools=None, timeout=600):
    """Returns (assistant_message_dict, usage_dict_or_None). Raises LLMError."""
    base = (profile.get("base_url") or "").rstrip("/")
    if not base:
        raise LLMError("The model has no base URL.")
    url = base + "/chat/completions"
    body = {"model": profile.get("model", ""), "messages": messages}
    if tools:
        body["tools"] = tools
    if profile.get("temperature") not in (None, ""):
        body["temperature"] = float(profile["temperature"])
    if (profile.get("reasoning_effort") or "").strip():
        body["reasoning_effort"] = profile["reasoning_effort"].strip()
    headers = {"Content-Type": "application/json"}
    key = config.resolve_key(profile)
    if key:
        headers["Authorization"] = "Bearer " + key
    data = json.dumps(body).encode("utf-8")

    payload = None
    for attempt in range(4):
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with net.urlopen(req, timeout) as r:
                payload = json.loads(r.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")[:1500]
            if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(2 * (attempt + 1))
                continue
            raise LLMError("HTTP %d from %s\n%s" % (e.code, url, text))
        except TimeoutError:
            raise LLMError("The model request timed out.")
        except (urllib.error.URLError, OSError) as e:
            raise LLMError("Cannot reach %s (%s)" % (url, getattr(e, "reason", e)))
        except ValueError:
            raise LLMError("The model server returned invalid JSON.")
    if not payload or payload.get("error"):
        raise LLMError("Model error: %s" % json.dumps((payload or {}).get("error"))[:800])
    try:
        msg = payload["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        raise LLMError("Unexpected response: " + json.dumps(payload)[:500])
    return _normalize(msg), payload.get("usage")
