# AI Gateway Solo

> 一个 Python 文件，让 Claude 桌面应用接入**任意 OpenAI 兼容模型**。无需 Docker、无需 One-API、无需配置数据库。

## 与完整版的关系

本项目是 [claude-3p-ai-gateway](https://github.com/liujunjiepeter/claude-3p-ai-gateway) 的**轻量独立版**。完整版适合多用户 SaaS/企业部署（Docker + One-API + 数据库 + 管理后台），本版本专为**个人用户和快速部署**设计。

| | 完整版 (Docker) | Solo (单文件) |
|---|---|---|
| 部署方式 | `docker-compose up -d` | `python3 proxy.py` |
| 依赖 | Docker + One-API + Python 镜像 | **仅 Python 3 标准库** |
| 协议翻译 | proxy.py | proxy.py（**同一套翻译引擎**） |
| 模型路由 | One-API 渠道管理 | BACKENDS 字典 |
| API Key 管理 | One-API 令牌系统 | 统一 Gateway Token + 后端 Key |
| 流式 SSE | ✅ | ✅ |
| 工具调用 | ✅ | ✅ |
| 多模态图片 | ✅ | ✅ |
| 推理模型 thinking | ✅ | ✅ |
| 孤立工具降级 | ✅ | ✅ |
| 多用户 | ✅ | ❌（单用户设计） |
| 用量看板 | ✅ | ❌ |
| 管理后台 | ✅ | ❌ |
| 启动时间 | ~15 秒 | **< 1 秒** |

**共用同一套协议翻译逻辑**——Solo 版从完整版的 proxy.py 抽出了所有 Anthropic ↔ OpenAI 翻译代码，去掉了对 One-API 的依赖，替换为内置的模型路由字典。

## 这是什么？

Claude 桌面应用（包括 Claude-3p）默认只能使用 Anthropic 自家的模型。本项目通过一个 480 行的 Python 脚本，让你在 Claude 界面右下角自由切换**任何 OpenAI 兼容模型**，**无需重启**。

```
┌──────────────────────────────────────────────────────────┐
│  Claude-3p 桌面应用                                        │
│  ┌────────────────────────────────────────────────────┐  │
│  │  模型选择:  [gpt-4o ▼] [deepseek ▼] [mimo ▼]       │  │
│  └────────────────────────────────────────────────────┘  │
│  统一 API Key: sk-my-unified-gateway-token                │
└──────────────────────┬───────────────────────────────────┘
                       │  Anthropic Messages API
                       ▼
┌──────────────────────────────────────────────────────────┐
│  proxy.py (480 行 Python)                                 │
│  ┌────────────────────────────────────────────────────┐  │
│  │  ① 鉴权: 验证统一 Gateway Token                      │  │
│  │  ② 翻译: Anthropic ↔ OpenAI 协议双向转换             │  │
│  │  ③ 路由: 根据模型名注入对应的真实 API Key             │  │
│  └────────────────────────────────────────────────────┘  │
└──────┬──────────┬──────────┬─────────────────────────────┘
       │          │          │
       ▼          ▼          ▼
   OpenAI     DeepSeek     MiMo     ...任意 OpenAI 兼容模型
   GPT-4o     V4 Pro       V2.5
```

## 功能特性

### 协议翻译覆盖

proxy.py 完整实现了 Anthropic Messages API 与 OpenAI Chat Completions API 的双向翻译：

**请求方向（Anthropic → OpenAI）**
- 文本消息：保持原样透传
- 图片块：`{type: "image", source: {...}}` → `{type: "image_url", image_url: {...}}`
- 工具定义：Anthropic `tools` 数组 → OpenAI `functions` 数组
- 推理内容保留：thinking 块 → `reasoning_content` 字段
- 工具结果：`tool_result` → OpenAI `role: "tool"` 消息
- 空结果防护：空工具结果自动填充占位文本
- 系统提示词：Anthropic `system` 参数 → OpenAI `role: "system"` 消息

**响应方向（OpenAI → Anthropic）**
- 文本内容：`content` → `{type: "text", text: ...}`
- 推理内容：`reasoning_content` → `{type: "thinking", thinking: ...}`
- 工具调用：`tool_calls` → `{type: "tool_use", id: ..., name: ..., input: ...}`
- 流式事件顺序：严格匹配 Anthropic SSE 规范

**流式 SSE 事件序列**

```
message_start          ← 包含 input_tokens 估算
content_block_start    ← thinking 块（推理模型）
ping                   ← 首块后立即发送
content_block_delta    ← thinking_delta × N
content_block_stop     ← 关闭 thinking
content_block_start    ← text 块（或 tool_use 块）
content_block_delta    ← text_delta × N（或 input_json_delta）
content_block_stop     ← 关闭
message_delta          ← stop_reason + usage
message_stop
```

### 统一 API Key 鉴权

Claude-3p 只填一个 key（`sk-my-unified-gateway-token`），真实的后端 Key 隐藏在你的机器上：

```
Claude-3p 请求 → Authorization: Bearer sk-my-unified-gateway-token
                      │
                      ▼
              proxy.py 验证 → 通过 → 查 BACKENDS 字典 → 注入真实的厂商 Key
                              → 拒绝 → 401 Unauthorized
```

即使有人拿到你的 Gateway Token，没有你的 `.env` 文件也访问不了后端模型。

### 健壮性

- **鉴权拦截**：请求开头验证 Gateway Token，无效直接 401
- **JSON 错误处理**：请求体解析失败返回 400
- **空 choices 防护**：流式解析跳过空块
- **孤立工具降级**：不完整 tool_calls 自动转为纯文本
- **多线程并发**：ThreadingMixIn 处理并发
- **超时控制**：后端请求 120 秒超时
- **错误流关闭**：流式异常时发送完整结束序列

## 快速开始

### 平台支持

| 平台 | Python 3 | 启动命令 |
|------|:---:|------|
| macOS | ✅ 系统自带 | `source .env && python3 proxy.py` |
| Windows | ✅ 需安装 | `.env && python proxy.py` |
| Linux | ✅ 系统自带 | `source .env && python3 proxy.py` |
| Docker | ✅ | 见「Docker 部署」 |

### 前提条件

- Python 3.9+
- 至少一个 OpenAI 兼容模型的 API Key
- Claude-3p 桌面应用

### 1. 克隆项目

```bash
git clone https://github.com/liujunjiepeter/ai-gateway-solo.git
cd ai-gateway-solo
```

### 2. 配置

```bash
cp .env.example .env
```

编辑 `.env`：

```bash
# 统一网关 Token —— Claude-3p 里填这个
PROXY_TOKEN=sk-my-unified-gateway-token

# 后端模型 API Key —— 真实厂商 Key，只存在你本地
DEEPSEEK_API_KEY=sk-your-deepseek-key-here
XIAOMI_API_KEY=tp-your-xiaomi-key-here
OPENAI_API_KEY=sk-your-openai-key-here     # 如有
GROQ_API_KEY=gsk-your-groq-key-here        # 如有
```

### 3. 启动

```bash
source .env && python3 proxy.py
```

输出：

```
🔀 AI Gateway Solo listening on :9999
  deepseek-v4-pro → https://api.deepseek.com/v1/chat/completions  ✅
  mimo-v2.5       → https://token-plan-cn.xiaomimimo.com/v1/chat/completions  ✅
```

### 4. 验证

```bash
curl http://localhost:9999/v1/models \
  -H "Authorization: Bearer sk-my-unified-gateway-token"
```

### 5. 配置 Claude-3p

将 `claude-3p-config.json` 安装到 Claude-3p 的配置目录：

```bash
# macOS
CONFIG_DIR="$HOME/Library/Application Support/Claude-3p/configLibrary"
UUID=$(uuidgen | tr '[:upper:]' '[:lower:]')
cp claude-3p-config.json "$CONFIG_DIR/$UUID.json"

# Windows PowerShell
$CONFIG_DIR="$env:APPDATA\Claude-3p\configLibrary"
$UUID = [guid]::NewGuid().ToString()
Copy-Item claude-3p-config.json "$CONFIG_DIR\$UUID.json"
```

编辑 `$CONFIG_DIR/_meta.json`，添加条目并设置 `appliedId`。

### 6. 重启 Claude-3p

右下角即可选择你配置的所有模型，随时切换。

## 添加更多模型

在 `proxy.py` 的 `BACKENDS` 字典中添加新条目，同时更新 `MODELS` 列表：

```python
# 第 30 行附近
BACKENDS = {
    "deepseek-v4-pro": {
        "url": "https://api.deepseek.com/v1/chat/completions",
        "key": os.environ.get("DEEPSEEK_API_KEY", ""),
    },
    "mimo-v2.5": {
        "url": "https://token-plan-cn.xiaomimimo.com/v1/chat/completions",
        "key": os.environ.get("XIAOMI_API_KEY", ""),
    },
    # ↓ 新增模型 ↓
    "gpt-4o": {
        "url": "https://api.openai.com/v1/chat/completions",
        "key": os.environ.get("OPENAI_API_KEY", ""),
    },
    "gemini-2.0-flash": {
        "url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "key": os.environ.get("GEMINI_API_KEY", ""),
    },
}

# 第 38 行附近
MODELS = [
    {"id": "deepseek-v4-pro", "type": "model", "display_name": "DeepSeek V4 Pro"},
    {"id": "mimo-v2.5", "type": "model", "display_name": "MiMo V2.5"},
    # ↓ 新增 ↓
    {"id": "gpt-4o", "type": "model", "display_name": "GPT-4o"},
    {"id": "gemini-2.0-flash", "type": "model", "display_name": "Gemini Flash"},
]
```

`.env` 里加对应的 API Key，重启 proxy 即可。

支持任意兼容 `/v1/chat/completions` 端点的模型供应商：

| 供应商 | 地址示例 |
|--------|----------|
| OpenAI | `https://api.openai.com/v1/chat/completions` |
| DeepSeek | `https://api.deepseek.com/v1/chat/completions` |
| Groq | `https://api.groq.com/openai/v1/chat/completions` |
| Together | `https://api.together.xyz/v1/chat/completions` |
| 阿里 Qwen | `https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions` |
| 小米 MiMo | `https://token-plan-cn.xiaomimimo.com/v1/chat/completions` |
| 硅基流动 | `https://api.siliconflow.cn/v1/chat/completions` |
| Gemini (OpenAI 兼容) | `https://generativelanguage.googleapis.com/v1beta/openai/chat/completions` |

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `PROXY_TOKEN` | 统一网关 Token（Claude-3p 填这个） | `sk-my-unified-gateway-token` |
| `DEEPSEEK_API_KEY` | DeepSeek API Key | - |
| `XIAOMI_API_KEY` | 小米 MiMo API Key | - |
| `OPENAI_API_KEY` | OpenAI API Key（如有） | - |

> SSL 证书与代理：
> Solo 版使用 Python `urllib` 标准库，自动使用系统证书。
> 如果通过代理上网，设置 `HTTP_PROXY` / `HTTPS_PROXY` 环境变量即可。

## 故障排查

### 401 Unauthorized

Claude-3p 的 API Key 填错了。确认跟 `.env` 里的 `PROXY_TOKEN` 一致。

### 模型不回复

```bash
# 1. 检查 proxy 是否在运行
lsof -i :9999

# 2. 直接测试
curl http://localhost:9999/v1/messages \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-my-unified-gateway-token" \
  -d '{"model":"deepseek-v4-pro","max_tokens":50,"messages":[{"role":"user","content":"hi"}]}'

# 3. 排查后端是否可达
curl https://api.deepseek.com/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $DEEPSEEK_API_KEY" \
  -d '{"model":"deepseek-v4-pro","max_tokens":10,"messages":[{"role":"user","content":"hi"}]}'
```

### 回复慢

- 推理模型有思考阶段，首字 2-5 秒正常
- 检查后端状态（快速测试：换一个模型试试）
- 非推理模型应在 1 秒内开始回复

### 工具调用不执行

```bash
# 查看是否有 tool_calls 被降级
grep "降级" /tmp/ai-gateway-solo.log
```

有降级日志 = 对话历史中有不完整的工具调用链，开新对话即可。

### 添加模型后不生效

更新 `BACKENDS` 和 `MODELS` 后需要**重启 proxy.py**（Ctrl+C 再启动）。

### 对话太长超时

累积数百条消息后建议开新对话。超时 120 秒。

## Docker 部署

虽然本版本不强制 Docker，但如果你想用 Docker：

```dockerfile
FROM python:3.13-alpine
COPY proxy.py /
ENV PROXY_TOKEN=sk-my-unified-gateway-token
EXPOSE 9999
CMD ["python3", "/proxy.py", "9999"]
```

```bash
docker build -t ai-gateway-solo .
docker run -d --env-file .env -p 9999:9999 ai-gateway-solo
```

## 项目结构

```
ai-gateway-solo/
├── .env.example          # 配置模板
├── .gitignore            # 排除 .env
├── README.md             # 本文档
├── claude-3p-config.json # Claude-3p 配置模板
└── proxy.py              # 全部逻辑：鉴权 + 翻译 + 路由（480 行）
```

## 工作原理

### 为什么一个文件就够了

Claude 桌面应用使用 Anthropic Messages API，第三方模型使用 OpenAI Chat Completions API。两者**格式完全不同**。proxy.py 负责所有转换：

| | Anthropic | OpenAI |
|---|---|---|
| 端点 | `POST /v1/messages` | `POST /v1/chat/completions` |
| 文本 | `[{type:"text", text}]` | `"string"` |
| 工具调用 | `{type:"tool_use", id, name, input}` | `tool_calls: [{function: {name, arguments}}]` |
| 图片 | `{type:"image", source: {data, media_type}}` | `{type:"image_url", image_url: {url}}` |
| 推理 | `{type:"thinking", thinking}` | `reasoning_content` |
| 流式 | `event: content_block_delta` | `data: {"choices":[{"delta":...}]}` |

完整版需要用 One-API 管理多渠道——适合多人、多供应商。Solo 版只有一个用户，直接写字典比配 One-API 快 10 倍。

### 流式状态机

```
         ┌──────────┐
         │  None    │
         └────┬─────┘
              │ reasoning_content
         ┌────▼─────┐
         │ thinking │ → content_block_start(thinking) → delta × N
         └────┬─────┘
              │ content
         ┌────▼─────┐
         │   text   │ → content_block_stop(thinking) → start(text) → delta × N
         └────┬─────┘
              │ tool_calls
         ┌────▼──────┐
         │ tool_use  │ → start(tool_use) → input_json_delta × N
         └────┬──────┘
              │ finish_reason
              ▼
         content_block_stop → message_delta → message_stop
```

### 请求处理流程

```
1. 鉴权       验证 Authorization: Bearer <PROXY_TOKEN>
                ↓ 不通过 → 401
2. 请求翻译    Anthropic Messages → OpenAI Chat Completions
                ↓
3. 模型路由    查 BACKENDS[model] → 获取后端 URL + 真实 Key
                ↓
4. 发送请求    用真实 Key 调用后端 API
                ↓
5. 响应翻译    OpenAI Chat Completions → Anthropic Messages (流式或非流式)
                ↓
6. 返回        SSE 事件流 / JSON 响应 → Claude-3p
```

## License


GPL 3.0 — 免费使用，衍生作品必须开源。

<!-- trigger refresh -->
