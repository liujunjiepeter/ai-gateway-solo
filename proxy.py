"""
AI Gateway Solo — Anthropic ↔ OpenAI protocol translation + model routing.
Zero dependencies beyond Python stdlib. Replaces One-API entirely.

Usage:
    DEEPSEEK_API_KEY=sk-xxx XIAOMI_API_KEY=tp-xxx python3 proxy.py
"""
import json, os, sys, uuid, re
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from urllib.parse import urlparse


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


# ── 统一网关鉴权 ──────────────────────────────────────────────────

GATEWAY_TOKEN = os.environ.get("PROXY_TOKEN", "sk-my-unified-gateway-token")

# ── 路由与配置 ──────────────────────────────────────────────────

BACKENDS = {
    "deepseek-v4-pro": {
        "url": "https://api.deepseek.com/v1/chat/completions",
        "key": os.environ.get("DEEPSEEK_API_KEY", ""),
        "capabilities": {"image": False, "audio": False, "video": False},
    },
    "mimo-v2.5-direct": {
        "url": "https://api.xiaomimimo.com/v1/chat/completions",
        "key": os.environ.get("XIAOMI_DIRECT_API_KEY", ""),
        "real_model": "mimo-v2.5",  # 发给 API 的实际模型名
        "capabilities": {"image": True, "audio": False, "video": False},
    },
    "mimo-v2.5-pro": {
        "url": "https://api.xiaomimimo.com/v1/chat/completions",
        "key": os.environ.get("XIAOMI_DIRECT_API_KEY", ""),
        "capabilities": {"image": False, "audio": False, "video": False},
    },
    "mimo-v2.5": {
        "url": "https://token-plan-cn.xiaomimimo.com/v1/chat/completions",
        "key": os.environ.get("XIAOMI_API_KEY", ""),
        "capabilities": {"image": True, "audio": False, "video": False},
    },
}

# 💡 名称映射表：接收客户端伪装名，映射回真实大模型名
MODEL_MAPPING = {
    "claude-3-5-sonnet-20241022": "deepseek-v4-pro",
    "claude-3-5-haiku-20241022": "mimo-v2.5",
    "claude-3-opus-20240229": "mimo-v2.5-pro",
    "claude-3-haiku-20240307": "mimo-v2.5-direct",
    "claude-sonnet-4-6": "deepseek-v4-pro",
    "claude-haiku-4-5-20251001": "mimo-v2.5",
}

# 💡 暴露给客户端的模型 ID 必须符合官方白名单
MODELS = [
    {"id": "claude-3-5-sonnet-20241022", "type": "model", "display_name": "DeepSeek V4 Pro"},
    {"id": "claude-3-5-haiku-20241022", "type": "model", "display_name": "MiMo V2.5 (Token Plan)"},
    {"id": "claude-3-opus-20240229", "type": "model", "display_name": "MiMo V2.5 Pro"},
    {"id": "claude-3-haiku-20240307", "type": "model", "display_name": "MiMo V2.5 (Direct)"},
    {"id": "claude-sonnet-4-6", "type": "model", "display_name": "DeepSeek V4 Pro"},
    {"id": "claude-haiku-4-5-20251001", "type": "model", "display_name": "MiMo V2.5 (Token Plan)"},
]

TEXT_ONLY_MODEL_HINTS = ("deepseek",)
REASONING_REPLAY_MODEL_HINTS = ("mimo",)


def _supports_vision(model: str) -> bool:
    model_l = (model or "").lower()
    return not any(hint in model_l for hint in TEXT_ONLY_MODEL_HINTS)


def _should_replay_reasoning(model: str) -> bool:
    model_l = (model or "").lower()
    return any(hint in model_l for hint in REASONING_REPLAY_MODEL_HINTS)


def _detect_media_types(messages: list) -> set:
    """扫描请求中所有媒体类型（image / audio / video）。"""
    types = set()
    for m in messages:
        content = m.get("content", "")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") in ("image", "audio", "video"):
                types.add(block["type"])
    return types


def _find_capable_fallback(requested_internal: str, required_media: set) -> str | None:
    """在 BACKENDS 中找一个具备 required_media 全能力的可用模型（跳过同名且 Key 已配的）。"""
    for name, cfg in BACKENDS.items():
        if not cfg.get("key"):
            continue
        caps = cfg.get("capabilities", {})
        if all(caps.get(mt, False) for mt in required_media):
            return name
    return None


def _content_to_text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(p for p in parts if p)
    return json.dumps(content, ensure_ascii=False)


def _downgrade_tool_message(msg: dict) -> dict:
    tool_id = msg.get("tool_call_id", "") or "unknown"
    content = _content_to_text(msg.get("content", ""))
    if not content.strip():
        content = "Task completed successfully (no output)."
    return {"role": "user", "content": f"[工具结果 {tool_id}]\n{content}"}


def _downgrade_assistant_tool_calls(msg: dict, reason: str) -> dict:
    downgraded = dict(msg)
    calls = downgraded.pop("tool_calls", []) or []
    names = ", ".join(
        (tc.get("function") or {}).get("name", "") for tc in calls if isinstance(tc, dict)
    ) or "unknown"
    content = _content_to_text(downgraded.get("content", ""))
    suffix = f"[工具调用 {names} 已省略：{reason}]"
    downgraded["content"] = f"{content}\n{suffix}".strip()
    return downgraded


def _sanitize_openai_messages(messages: list) -> list:
    """Keep tool-call history valid for OpenAI-compatible APIs."""
    sanitized = []
    i = 0

    while i < len(messages):
        msg = messages[i]
        role = msg.get("role")

        if role == "tool":
            sanitized.append(_downgrade_tool_message(msg))
            i += 1
            continue

        if role == "assistant" and msg.get("tool_calls"):
            expected = [
                tc.get("id", "")
                for tc in msg.get("tool_calls", [])
                if isinstance(tc, dict) and tc.get("id")
            ]
            expected_set = set(expected)
            tool_msgs = []
            j = i + 1
            while j < len(messages) and messages[j].get("role") == "tool":
                tool_msgs.append(messages[j])
                j += 1

            found_set = {
                tm.get("tool_call_id", "")
                for tm in tool_msgs
                if isinstance(tm, dict) and tm.get("tool_call_id")
            }

            if expected_set and expected_set.issubset(found_set):
                sanitized.append(msg)
                emitted = set()
                for tm in tool_msgs:
                    tool_id = tm.get("tool_call_id", "")
                    if tool_id in expected_set and tool_id not in emitted:
                        sanitized.append(tm)
                        emitted.add(tool_id)
                    else:
                        sanitized.append(_downgrade_tool_message(tm))
            else:
                sanitized.append(_downgrade_assistant_tool_calls(msg, "历史中缺少匹配的工具结果"))
                for tm in tool_msgs:
                    sanitized.append(_downgrade_tool_message(tm))
            i = j
            continue

        sanitized.append(msg)
        i += 1

    return sanitized


def get_backend(model: str) -> dict:
    """Resolve model name to backend config. Falls back to first available."""
    real_model = MODEL_MAPPING.get(model, model)  # 💡 自动把伪装名转回真实名
    if real_model in BACKENDS and BACKENDS[real_model]["key"]:
        return BACKENDS[real_model]
    for m, cfg in BACKENDS.items():
        if cfg["key"]:
            return cfg
    raise RuntimeError("No backend configured — set DEEPSEEK_API_KEY or XIAOMI_API_KEY")


# ── 协议核心转换 ─────────────────────────────────────────────────────

def anth_to_openai(body: dict) -> dict:
    client_model = body.get("model", "")
    model = MODEL_MAPPING.get(client_model, client_model)  # 💡 获取真实底层模型名

    supports_vision = _supports_vision(model)
    replay_reasoning = _should_replay_reasoning(model)
    messages = body.get("messages", [])
    system = body.get("system", None)
    max_tokens = body.get("max_tokens", 4096)
    temperature = body.get("temperature", 0.7)

    oai_messages = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")

        if isinstance(content, list):
            text_parts = []
            thinking_parts = []
            image_urls = []
            tool_calls_oai = []
            tool_results = []

            for block in content:
                if not isinstance(block, dict):
                    continue
                bt = block.get("type", "")
                if bt == "text":
                    text_parts.append(block.get("text", ""))
                elif bt == "thinking":
                    # 历史中的 thinking 必须保留，跨模型切换时 API 可能要求 reasoning_content 连贯
                    thinking_parts.append(block.get("thinking", ""))
                elif bt == "image":
                    if not supports_vision:
                        text_parts.append("\n[系统提示：图片已由网关自动过滤，因为当前模型不支持视觉输入]")
                    else:
                        source = block.get("source", {})
                        image_urls.append({
                            "type": "image_url",
                            "image_url": {"url": f"data:{source.get('media_type', 'image/jpeg')};base64,{source.get('data', '')}"},
                        })
                elif bt == "tool_use":
                    tool_calls_oai.append({
                        "id": block.get("id", ""),
                        "type": "function",
                        "function": {"name": block.get("name", ""), "arguments": json.dumps(block.get("input", {}))},
                    })
                elif bt == "tool_result":
                    content_val = block.get("content", "")
                    if isinstance(content_val, list):
                        parts = [c.get("text", "") for c in content_val if isinstance(c, dict) and c.get("type") == "text"]
                        content_val = "\n".join(parts) if parts else json.dumps(content_val)
                    elif not isinstance(content_val, str):
                        content_val = json.dumps(content_val)
                    if not content_val or content_val.strip() == "":
                        content_val = "Task completed successfully (no output)."

                    # 💡 截断防卡死：强制截断过长的隐形工具输出
                    if len(content_val) > 15000:
                        content_val = content_val[:15000] + "\n...[Warning: Output truncated by gateway to preserve KV Cache and token limits]..."

                    tool_results.append({"tool_call_id": block.get("tool_use_id", ""), "content": content_val})

            reasoning_text = "\n".join(thinking_parts) if thinking_parts else None

            if role == "assistant":
                msg = {"role": "assistant"}
                msg["content"] = "\n".join(text_parts) if text_parts else ""
                if tool_calls_oai:
                    msg["tool_calls"] = tool_calls_oai
                if reasoning_text:
                    msg["reasoning_content"] = reasoning_text
                elif replay_reasoning:
                    msg["reasoning_content"] = ""
                oai_messages.append(msg)
            elif role == "user":
                for tr in tool_results:
                    oai_messages.append({"role": "tool", "tool_call_id": tr["tool_call_id"], "content": tr["content"]})
                if text_parts or image_urls:
                    u_msg = {"role": "user"}
                    if image_urls:
                        u_msg["content"] = [*[{"type": "text", "text": t} for t in text_parts], *image_urls]
                    else:
                        u_msg["content"] = "\n".join(text_parts)
                    oai_messages.append(u_msg)
        else:
            msg = {"role": role, "content": content}
            if role == "assistant" and replay_reasoning:
                msg["reasoning_content"] = ""
            oai_messages.append(msg)

    if system:
        sys_text = ""
        if isinstance(system, str):
            sys_text = system
        elif isinstance(system, list):
            sys_text = "\n".join(b.get("text", "") for b in system if isinstance(b, dict) and b.get("type") == "text")

        if sys_text:
            # 💡 冻结时间：正则抹平 Claude 注入的动态时间戳，完美命中前缀缓存！
            sys_text = re.sub(r"Current time is.*?\n", "[Time frozen to preserve KV Cache]\n", sys_text, flags=re.IGNORECASE)
            oai_messages.insert(0, {"role": "system", "content": sys_text})

    sanitized = _sanitize_openai_messages(oai_messages)

    # 💡 直接带着真实的模型名字向厂家发请求（real_model 覆盖内部名）
    api_model = BACKENDS.get(model, {}).get("real_model", model)
    oai_body = {"model": api_model, "messages": sanitized, "max_tokens": max_tokens,
                "temperature": temperature, "stream": body.get("stream", False)}

    tools = body.get("tools")
    if tools:
        oai_tools = [{"type": "function", "function": {"name": t.get("name", ""),
                       "description": t.get("description", ""),
                       "parameters": t.get("input_schema", {})}} for t in tools]
        oai_body["tools"] = oai_tools
        tool_choice = body.get("tool_choice")
        if tool_choice and isinstance(tool_choice, dict):
            if tool_choice.get("type") == "any":
                oai_body["tool_choice"] = "required"
            elif tool_choice.get("type") == "tool":
                oai_body["tool_choice"] = {"type": "function", "function": {"name": tool_choice.get("name", "")}}

    return oai_body


def openai_to_anth(resp: dict, model: str) -> dict:
    choices = resp.get("choices") or [{}]
    choice = choices[0] if choices else {}
    oai_msg = choice.get("message", {})
    content = oai_msg.get("content", "") or ""
    reasoning = oai_msg.get("reasoning_content", "") or ""
    tool_calls = oai_msg.get("tool_calls") or []

    finish = choice.get("finish_reason", "stop")
    stop_reason = "end_turn"
    if finish == "length":
        stop_reason = "max_tokens"
    elif finish == "tool_calls" or tool_calls:
        stop_reason = "tool_use"

    blocks = []
    if reasoning:
        blocks.append({"type": "thinking", "thinking": reasoning, "signature": ""})
    if content:
        blocks.append({"type": "text", "text": content})
    for tc in tool_calls:
        fn = tc.get("function", {})
        args_str = fn.get("arguments", "{}")
        try:
            args = json.loads(args_str) if isinstance(args_str, str) else args_str
        except json.JSONDecodeError:
            args = {}
        blocks.append({"type": "tool_use", "id": tc.get("id", f"toolu_{uuid.uuid4().hex[:24]}"),
                       "name": fn.get("name", ""), "input": args})
    if not blocks:
        blocks = [{"type": "text", "text": ""}]

    return {"id": resp.get("id", f"msg_{uuid.uuid4().hex[:24]}"), "type": "message",
            "role": "assistant", "model": model, "content": blocks, "stop_reason": stop_reason,
            "stop_sequence": None, "usage": {"input_tokens": resp.get("usage", {}).get("prompt_tokens", 0),
                                              "output_tokens": resp.get("usage", {}).get("completion_tokens", 0)}}


# ── 流式直连 ────────────────────────────────────────────────────────

def stream_to_backend(body: dict):
    oai_body = anth_to_openai(body)
    oai_body["stream"] = True

    backend = get_backend(body.get("model", ""))
    req = Request(backend["url"], data=json.dumps(oai_body).encode(),
                  headers={"Content-Type": "application/json", "Authorization": f"Bearer {backend['key']}"})

    block_idx = 0
    phase = None
    finished = False
    pinged = False
    tool_states = {}

    try:
        with urlopen(req, timeout=120) as resp:
            for line in resp:
                line = line.decode().strip()
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str == "[DONE]":
                    if not finished:
                        yield from _emit_stream_close(phase, block_idx)
                    break
                try:
                    chunk = json.loads(data_str)
                    choices = chunk.get("choices")
                    if not choices:
                        continue
                    choice = choices[0]
                    delta = choice.get("delta", {})
                    reasoning = delta.get("reasoning_content", "")
                    content = delta.get("content", "")

                    if reasoning:
                        if phase != "thinking":
                            yield _event("content_block_start", {"type": "content_block_start", "index": block_idx,
                                        "content_block": {"type": "thinking", "thinking": "", "signature": ""}})
                            if not pinged:
                                yield f"event: ping\ndata: {json.dumps({'type': 'ping'})}\n\n"
                                pinged = True
                            phase = "thinking"
                        yield _event("content_block_delta", {"type": "content_block_delta", "index": block_idx,
                                     "delta": {"type": "thinking_delta", "thinking": reasoning}})

                    if content:
                        if phase == "thinking":
                            yield _event("content_block_stop", {"type": "content_block_stop", "index": block_idx})
                            block_idx = 1
                            phase = "text"
                            yield _event("content_block_start", {"type": "content_block_start", "index": block_idx,
                                         "content_block": {"type": "text", "text": ""}})
                        elif phase != "text" and phase != "tool_use":
                            phase = "text"
                            yield _event("content_block_start", {"type": "content_block_start", "index": block_idx,
                                         "content_block": {"type": "text", "text": ""}})
                            if not pinged:
                                yield f"event: ping\ndata: {json.dumps({'type': 'ping'})}\n\n"
                                pinged = True
                        yield _event("content_block_delta", {"type": "content_block_delta", "index": block_idx,
                                     "delta": {"type": "text_delta", "text": content}})

                    for tc in (delta.get("tool_calls") or []):
                        idx = tc.get("index", 0)
                        if idx not in tool_states:
                            if phase == "thinking":
                                yield _event("content_block_stop", {"type": "content_block_stop", "index": block_idx})
                                block_idx += 1
                                yield _event("content_block_start", {"type": "content_block_start", "index": block_idx, "content_block": {"type": "text", "text": "\n"}})
                                yield _event("content_block_stop", {"type": "content_block_stop", "index": block_idx})
                            elif phase:
                                yield _event("content_block_stop", {"type": "content_block_stop", "index": block_idx})
                            block_idx += 1
                            phase = "tool_use"
                            ts_id = tc.get("id") or f"toolu_{uuid.uuid4().hex[:24]}"
                            tool_states[idx] = {"id": ts_id, "name": "", "args_str": "", "started": False}
                        ts = tool_states[idx]
                        if tc.get("id"):
                            ts["id"] = tc["id"]
                        fn = tc.get("function", {})
                        if fn.get("name") and not ts["name"]:
                            ts["name"] = fn["name"]
                        if ts["name"] and not ts.get("started"):
                            yield _event("content_block_start", {"type": "content_block_start", "index": block_idx,
                                         "content_block": {"type": "tool_use", "id": ts["id"], "name": ts["name"], "input": {}}})
                            ts["started"] = True
                        if fn.get("arguments"):
                            ts["args_str"] += fn["arguments"]
                            yield _event("content_block_delta", {"type": "content_block_delta", "index": block_idx,
                                         "delta": {"type": "input_json_delta", "partial_json": fn["arguments"]}})

                    if choice.get("finish_reason"):
                        if phase == "thinking":
                            yield _event("content_block_stop", {"type": "content_block_stop", "index": block_idx})
                            block_idx += 1
                            yield _event("content_block_start", {"type": "content_block_start", "index": block_idx, "content_block": {"type": "text", "text": "\n"}})
                            phase = "text"
                        yield from _emit_stream_close(phase, block_idx)
                        finished = True

                except (json.JSONDecodeError, IndexError, KeyError):
                    continue
    except (HTTPError, OSError) as e:
        err_body = e.read().decode() if hasattr(e, "read") else str(e)
        if not finished:
            yield from _emit_stream_close(phase, block_idx)
        yield f"event: error\ndata: {json.dumps({'type': 'error', 'error': {'type': 'api_error', 'message': f'Upstream error: {err_body}'}})}\n\n"


def _event(name: str, data: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(data)}\n\n"


def _emit_stream_close(phase: str, block_idx: int):
    ev = _close_current_block(phase, block_idx)
    if ev:
        yield ev
    stop_reason = "tool_use" if phase == "tool_use" else "end_turn"
    yield f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': stop_reason}, 'usage': {'output_tokens': 0}})}\n\n"
    yield f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'})}\n\n"


def _close_current_block(phase: str, block_idx: int):
    if phase in ("thinking", "text", "tool_use"):
        return _event("content_block_stop", {"type": "content_block_stop", "index": block_idx})
    return ""


def _estimate_tokens(body: dict) -> int:
    total = 0
    for msg in body.get("messages", []):
        content = msg.get("content", "")
        if isinstance(content, str):
            total += max(len(content) // 3, 1)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    total += max(len(block.get("text", "")) // 3, 1)
    system = body.get("system", "")
    if isinstance(system, str):
        total += max(len(system) // 3, 1)
    elif isinstance(system, list):
        for b in system:
            if isinstance(b, dict) and b.get("type") == "text":
                total += max(len(b.get("text", "")) // 3, 1)
    return max(total, 1)


# ── HTTP 服务层 ─────────────────────────────────────────────────────

class ProxyHandler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")

    def _auth_check(self) -> bool:
        auth = self.headers.get("Authorization", "")
        return auth == f"Bearer {GATEWAY_TOKEN}"

    def _send_unauthorized(self):
        self.send_response(401)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"type": "error", "error": {"type": "authentication_error", "message": "Invalid Gateway Token"}}).encode())

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_POST(self):
        if not self._auth_check():
            return self._send_unauthorized()

        path = urlparse(self.path).path
        if path == "/v1/messages":
            self._handle_messages()
        elif path == "/v1/messages/count_tokens":
            self._handle_count_tokens()

    def do_GET(self):
        if not self._auth_check():
            return self._send_unauthorized()

        path = urlparse(self.path).path
        if path == "/v1/models":
            self._handle_models()

    def _handle_messages(self):
        content_len = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(content_len))
        except (json.JSONDecodeError, ValueError):
            self.send_response(400)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"type": "error", "error": {"type": "invalid_request_error", "message": "Invalid JSON body"}}).encode())
            return

        # ── 多媒体能力检测 & fallback ──────────────────────────────
        original_model = body.get("model", "")  # 客户端原始模型名，响应里用这个
        client_model = original_model
        internal_model = MODEL_MAPPING.get(client_model, client_model)
        media_types = _detect_media_types(body.get("messages", []))
        if media_types:
            backend = BACKENDS.get(internal_model, {})
            backend_caps = backend.get("capabilities", {})
            missing = [mt for mt in media_types if not backend_caps.get(mt, False)]
            if missing:
                fallback = _find_capable_fallback(internal_model, media_types)
                if not fallback:
                    self.send_response(400)
                    self._cors()
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({
                        "type": "error",
                        "error": {"type": "invalid_request_error",
                                  "message": f"无法理解多媒体内容（{'/'.join(sorted(missing))}）。当前已配置的模型均不支持。"}
                    }).encode())
                    return
                # 找到 Claude 伪装名 → 替换 body.model
                new_client = next((k for k, v in MODEL_MAPPING.items() if v == fallback), client_model)
                print(f"[proxy] 🔀 多媒体请求重路由：{client_model} → {new_client}（{', '.join(sorted(media_types))}）→ {fallback}")
                body["model"] = new_client

        if body.get("stream"):
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()

            msg_id = f"msg_{uuid.uuid4().hex[:24]}"
            start = {"type": "message_start", "message": {"id": msg_id, "type": "message", "role": "assistant",
                     "model": original_model, "content": [], "stop_reason": None, "stop_sequence": None,
                     "usage": {"input_tokens": _estimate_tokens(body), "output_tokens": 0}}}
            self.wfile.write(f"event: message_start\ndata: {json.dumps(start)}\n\n".encode())

            for chunk_str in stream_to_backend(body):
                self.wfile.write(chunk_str.encode())
                self.wfile.flush()
        else:
            oai_body = anth_to_openai(body)
            backend = get_backend(body.get("model", ""))
            req = Request(backend["url"], data=json.dumps(oai_body).encode(),
                          headers={"Content-Type": "application/json", "Authorization": f"Bearer {backend['key']}"})
            try:
                with urlopen(req, timeout=120) as resp:
                    oai_resp = json.loads(resp.read())
                anth_resp = openai_to_anth(oai_resp, original_model)
                self.send_response(200)
                self._cors()
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(anth_resp).encode())
            except (HTTPError, json.JSONDecodeError, OSError) as e:
                err = e.read().decode() if hasattr(e, "read") else str(e)
                self.send_response(getattr(e, "code", 502))
                self._cors()
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"type": "error", "error": {"type": "api_error", "message": err}}).encode())

    def _handle_count_tokens(self):
        content_len = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(content_len))
        except (json.JSONDecodeError, ValueError):
            self.send_response(400)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Invalid JSON"}).encode())
            return
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"input_tokens": _estimate_tokens(body)}).encode())

    def _handle_models(self):
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"data": MODELS}).encode())

    def log_message(self, format, *args):
        sys.stderr.write(f"[proxy] {args[0]}\n")


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9999
    print(f"🔀 AI Gateway Solo listening on :{port}")
    for m, cfg in BACKENDS.items():
        status = "✅" if cfg["key"] else "❌ 缺少 API Key"
        print(f"  {m} → {cfg['url']}  {status}")
    ThreadingHTTPServer(("0.0.0.0", port), ProxyHandler).serve_forever()
