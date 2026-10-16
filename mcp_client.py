"""Minimal MCP client for the 'streamable HTTP' transport (what Binary Ninja's GUI server speaks).

Only what the agent needs: initialize, tools/list, tools/call. Replies may be plain JSON or an
SSE stream; both are handled. No third-party dependencies.

IMPORTANT: never call this from the Qt main thread. The GUI server may need the main thread to
answer, so blocking it would deadlock Binary Ninja.
"""
import json
import urllib.error
import urllib.request

from . import net

PROTOCOL_VERSION = "2025-06-18"
_NOMATCH = object()


class McpError(Exception):
    pass


class _SessionExpired(McpError):
    pass


class McpClient:
    def __init__(self, url, token="", timeout=900):
        self.url = url
        self.token = token or ""
        self.timeout = timeout
        self.session = None
        self.protocol = PROTOCOL_VERSION
        self._ready = False
        self._id = 0

    # ---------------------------------------------------------------- transport
    def _headers(self):
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.token:
            h["Authorization"] = "Bearer " + self.token
        if self.session:
            h["Mcp-Session-Id"] = self.session
        if self._ready:
            h["MCP-Protocol-Version"] = self.protocol
        return h

    @staticmethod
    def _match(msg, want_id):
        if isinstance(msg, list):
            for m in msg:
                r = McpClient._match(m, want_id)
                if r is not _NOMATCH:
                    return r
            return _NOMATCH
        if not isinstance(msg, dict) or "method" in msg or msg.get("id") != want_id:
            return _NOMATCH
        if "error" in msg:
            e = msg["error"] or {}
            raise McpError("MCP error %s: %s" % (e.get("code"), e.get("message")))
        return msg.get("result", {})

    def _read_sse(self, resp, want_id):
        data = []

        def flush():
            if not data:
                return _NOMATCH
            payload = "\n".join(data)
            data.clear()
            try:
                return self._match(json.loads(payload), want_id)
            except ValueError:
                return _NOMATCH

        for raw in resp:
            line = raw.decode("utf-8").rstrip("\r\n")
            if line == "":
                r = flush()
                if r is not _NOMATCH:
                    return r
            elif line.startswith("data:"):
                v = line[5:]
                data.append(v[1:] if v.startswith(" ") else v)
        r = flush()
        if r is not _NOMATCH:
            return r
        raise McpError("MCP stream ended without a response")

    def _post(self, message, want_id=None):
        req = urllib.request.Request(
            self.url, data=json.dumps(message).encode("utf-8"), headers=self._headers(), method="POST"
        )
        try:
            resp = net.urlopen(req, self.timeout)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:500]
            if e.code == 404 and self.session:
                raise _SessionExpired("MCP session expired")
            if e.code in (401, 403):
                raise McpError("The MCP server rejected the request (HTTP %d). Check the token (ui.mcp.token)." % e.code)
            raise McpError("MCP HTTP %d: %s" % (e.code, body))
        except TimeoutError:
            raise McpError("MCP request timed out")
        except (urllib.error.URLError, OSError) as e:
            raise McpError(
                "Cannot reach the Binary Ninja MCP server at %s (%s). Enable ui.mcp.enabled, "
                "restart, then run Plugins > MCP > Start Server." % (self.url, getattr(e, "reason", e))
            )
        with resp:
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                self.session = sid
            if want_id is None:
                resp.read()
                return None
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "text/event-stream" in ctype:
                return self._read_sse(resp, want_id)
            body = resp.read().decode("utf-8")
        try:
            r = self._match(json.loads(body), want_id)
        except ValueError:
            raise McpError("MCP server sent invalid JSON: " + body[:200])
        if r is _NOMATCH:
            raise McpError("MCP server sent no matching response")
        return r

    def request(self, method, params=None):
        for attempt in (0, 1):
            self._id += 1
            msg = {"jsonrpc": "2.0", "id": self._id, "method": method}
            if params is not None:
                msg["params"] = params
            try:
                return self._post(msg, self._id)
            except _SessionExpired:
                if attempt:
                    raise
                self.session, self._ready = None, False
                self.initialize()

    # ---------------------------------------------------------------- protocol
    def initialize(self):
        res = self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "ninjaprompt", "version": "0.1.0"},
            },
        )
        self.protocol = res.get("protocolVersion") or PROTOCOL_VERSION
        self._ready = True
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return res

    def list_tools(self):
        tools, cursor = [], None
        while True:
            res = self.request("tools/list", {"cursor": cursor} if cursor else {})
            tools.extend(res.get("tools") or [])
            cursor = res.get("nextCursor")
            if not cursor:
                return tools

    def call_tool(self, name, arguments):
        """Returns (text, is_error)."""
        res = self.request("tools/call", {"name": name, "arguments": arguments or {}})
        parts = []
        for c in res.get("content") or []:
            t = c.get("type")
            if t == "text":
                parts.append(c.get("text", ""))
            elif t == "image":
                parts.append("[image omitted]")
            else:
                parts.append("[%s content omitted]" % t)
        text = "\n".join(parts)
        if not text and res.get("structuredContent") is not None:
            text = json.dumps(res["structuredContent"], indent=1)
        return text, bool(res.get("isError"))

    def close(self):
        if not self.session:
            return
        try:
            req = urllib.request.Request(self.url, headers=self._headers(), method="DELETE")
            net.urlopen(req, 5).close()
        except Exception:
            pass
