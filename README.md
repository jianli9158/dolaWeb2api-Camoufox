# dolaWeb2api-Camoufox

> 基于 **原生 Camoufox + 纯 Python WebDriver BiDi** 的 **Dola 国际版（`https://www.dola.com/`）** OpenAI 兼容 API 网关。  
> 零 Playwright / Chromium 依赖，采用 `flow2api-Camoufox` 同源架构：**单账号独立隔离资料、每账号专属代理、Web LiveView 远程交互浏览器、Seedream 图像与 Seedance 视频模型支持**。

---

## 核心特性

- **原生 Camoufox + WebDriver BiDi 引擎（零 Playwright）**
  - 内置纯异步 WebDriver BiDi 协议客户端（`bidi_transport.py`），直接通过 `BiDiChannel`（`script.message`）在页面内执行带 `bdms.frontierSign` 签名的 `fetch()` 流式 SSE 对话，支持客户端断开自动 `AbortController.abort()`。
  - 自动定位或下载原生 Camoufox 二进制文件，按账号持久化锁定 Windows Firefox 指纹、字体、WebGL 配置（`camoufox_fingerprint.py` + `camoufox_assets/`）。
  - OS 内核级文件锁（`fcntl.flock` / `msvcrt.locking`）保护每个账号的 Profile，防止多进程并发损坏数据，并自动维护 `startupCache` 与冗余缓存清理（`camoufox_profile.py`）。

- **单账号完全隔离资料 & 统一多账号管理台（参考 `flow2api-Camoufox`）**
  - **启动零弹窗**：服务启动时绝不自动弹出浏览器窗口；无账号时保持空闲，有账号时仅在后台无头运行活跃账号。
  - **严格资料隔离**：每个账号拥有完全独立的隔离目录 `~/.dola_browser/accounts/<account_id>/`（独立 `firefox-profile/`、独立指纹、独立代理、独立锁），彻底杜绝多账号串号。
  - **纯净新增账号（Web LiveView）**：在 `/admin` 点击 **「🌐 浏览器登录新增账号」** 时，可提前指定账号名与专属代理，系统会在全新临时目录 `_staging/login_<id>/` 启动纯净 Camoufox（零旧号 Cookie 残留），在网页内直接完成 Google / Lark / 邮箱登录并自动落盘至独立隔离目录。
  - **单账号资料管理**：每个账号行提供 **「🖥️ 管理资料」** 按钮，自动释放运行时锁并以**该账号自己的隔离 Profile 与代理**打开远程交互浏览器，用于查看历史、重登或过人机验证。
  - **多账号智能调度**：支持 **故障切换（Failover）** 与 **轮询调度（Round-Robin）**，内置按能力（`chat` / `image` / `video` / `global`）独立冷却退避与自动故障转移。

- **全局与单账号代理支持（内置 `LocalSocks5Relay`）**
  - 支持 `http://`、`https://`、`socks5://`、`socks5h://`、`user:pass@host:port`、`host:port:user:pass`、`st5 host:port:user:pass`。
  - 带账号密码的代理与 SOCKS5H 代理自动经本地无认证 `LocalSocks5Relay` 透明转发给 Camoufox，同时透传至 `httpx`。
  - 日志与错误响应自动对 `sessionid`、`sid_tt`、`msToken`、`a_bogus`、`Bearer` 及代理密码进行脱敏（`redaction.py`）。

- **完整 OpenAI 兼容接口 & Dola 国际版模型**
  - **对话 & 思维链**：`/v1/chat/completions`（流式 SSE / 非流式 / 多轮上下文 / 思维链 `reasoning_content` / 多模态图片识图 / Tool Calling）。
  - **图像生成 & 编辑（Seedream）**：`/v1/images/generations`、`/v1/images/edits`、`/v1/images/upload`（通过 Dola 原生 `/chat/completion` `chat_ability.ability_type=3` 与海外新加坡 ImageX 上传）。
  - **视频生成（Seedance）**：`/v1/video/generations`（通过 Dola 原生 `/chat/completion` `chat_ability.ability_type=17` 与 `/chat/async/chunk_stream` 异步拉流，支持 `5s` / `10s` 与多比例）。
  - **即用即焚模式**：默认在 API 响应结束后自动调用 `/im/conversation/batch_del_user_conv` 删除临时对话，保持 Dola 账号侧边栏整洁（可在控制台顶栏或环境变量一键切换）。

---

## 支持的模型列表（`GET /v1/models`）

| 类型 | 模型 ID | Dola 上游能力 / 模型 | 说明 |
| :--- | :--- | :--- | :--- |
| 对话 | `dola`（默认）、`dola-pro`、`dola-turbo` | 标准对话 | 快速响应、支持多模态图片识图与多轮上下文 |
| 深度思考 | `dola-think` | Deep Think | 流式/非流式输出 `reasoning_content` 思维链 |
| 专家模式 | `dola-expert` | Expert Mode | 复杂推理模式 |
| 图像生成 | `seedream-4.5`（默认）、`seedream-5.0-pro`、`dola-image` | **Seedream 4.5** / **Seedream 5.0 Pro** | 支持 `ratio`（`auto`/`1:1`/`16:9`/`9:16`）与参考图编辑 |
| 视频生成 | `seedance-2.0-fast`（默认）、`seedance-2.0`、`seedance-1.0`、`dola-video` | **Dreamina Seedance 2.0 Fast** / **Seedance 1.0** | 支持文生视频/图生视频、`ratio` 与 `duration`（`5`/`10` 秒） |
| 兼容别名 | `doubao*`、`gpt-4o`、`claude-3-5-sonnet`、`deepseek-r1`、`dall-e-3`、`gpt-image-1` | 自动映射 | 兼容现有 OpenAI / Doubao 客户端无缝接入 |

---

## 快速开始

### 方式一：Docker Compose 部署（推荐）

```bash
cp .env.example .env
# 按需编辑 .env（建议生产环境配置 DOLA_API_KEY）
docker compose up -d --build
```

启动后访问管理控制台：`http://<你的服务器IP>:9090/admin`，点击 **「🌐 浏览器登录新增账号」**，填写账号名和代理（可选），在内置 Web LiveView 画布中直接登录 Dola 即可使用，全程无需 VNC 或桌面环境。

### 方式二：本地 Python 运行

要求 Python `>= 3.10`：

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
python -m doubao2api
# 或 pip install -e . 后直接运行:
# dolaweb2api
```

打开浏览器访问 `http://127.0.0.1:9090/admin` 添加账号并测试 API。

---

## API 调用示例

### 1. 标准对话与深度思考（流式）

```bash
curl http://127.0.0.1:9090/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $DOLA_API_KEY" \
  -d '{
    "model": "dola-think",
    "stream": true,
    "messages": [{"role": "user", "content": "What is 17 * 23?"}]
  }'
```

### 2. 多模态视觉识图

```bash
curl http://127.0.0.1:9090/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "dola",
    "messages": [{
      "role": "user",
      "content": [
        {"type": "text", "text": "Describe this image."},
        {"type": "image_url", "image_url": {"url": "https://example.com/photo.png"}}
      ]
    }]
  }'
```

### 3. Seedream 图像生成

```bash
curl http://127.0.0.1:9090/v1/images/generations \
  -H "Content-Type: application/json" \
  -d '{
    "model": "seedream-4.5",
    "prompt": "A minimalist pastel sunrise over Mount Fuji",
    "ratio": "16:9"
  }'
```

### 4. Seedance 视频生成

```bash
curl http://127.0.0.1:9090/v1/video/generations \
  -H "Content-Type: application/json" \
  -d '{
    "model": "seedance-2.0-fast",
    "prompt": "Gentle ocean waves glowing under golden sunset light",
    "ratio": "16:9",
    "duration": 5
  }'
```

---

## 环境变量说明

所有 `DOLA_*` 变量均向下兼容原有 `DOUBAO_*` 命名：

| 变量名 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `DOLA_HOST` | `0.0.0.0`（无 Key 时降级 `127.0.0.1`） | 监听地址（公网无 Key 需显式 `ALLOW_UNPROTECTED_BIND=true`） |
| `DOLA_PORT` | `9090` | 监听端口 |
| `DOLA_API_KEY` | 空 | 鉴权密钥（为空时不启用鉴权） |
| `DOLA_HEADLESS` | `true` | 运行时无头模式（`true` 不弹出桌面窗口；`false` 弹出窗口） |
| `DOLA_LAZY_START` | `false` | `true` 表示启动时不预热浏览器，按需拉起 |
| `DOLA_PROXY` | 空 | 全局默认出口代理（单账号专属代理优先于全局配置） |
| `DOLA_ACCOUNT_STRATEGY` | `failover` | 多账号调度策略：`failover` / `round_robin` |
| `DOLA_BROWSER_DATA` | `~/.dola_browser` | 隔离账号资料根目录（子目录 `accounts/<account_id>/`） |
| `CAMOUFOX_PATH` | 自动探测/下载 | 可选：手动指定已有 Camoufox 可执行文件路径 |
| `DOLA_LOCALE` | `en-US` | Camoufox 指纹语言 |
| `DOLA_TIMEZONE` | `America/New_York` | Camoufox 指纹时区 |
| `DOLA_AUTO_DELETE_CONV` | `true` | `true` 开启即用即焚，自动删除临时对话 |
| `DOLA_RPM_LIMIT` | `30` | 每分钟限流次数 |

---

## 测试与开发

```bash
pytest -q
```

默认自动化测试套件涵盖：
- Dola 协议参数与 `bdms` 请求头、Seedream / Seedance `chat_ability` 载荷
- WebDriver BiDi 编解码与 `BiDiChannel` 流取消、`LocalSocks5Relay`、OS 级 `ProfileLease`
- 多账号隔离目录、单账号代理与分能力冷却退避、故障切换与回滚
- 敏感凭证日志脱敏、Web LiveView 与隔离登录会话管理接口

---

## 致谢与声明

- 浏览器运行时、隔离资料管理、Web LiveView 与代理中继架构参考 [flow2api-Camoufox](https://github.com/jianli9158/flow2api-Camoufox)。
- 初版 OpenAI 协议桥接思路致谢开源社区 `doubao2api` 相关项目。
- 本项目采用 [Apache-2.0 License](LICENSE) 开源协议，仅供编程学习、浏览器自动化与个人研究使用，请遵守目标平台服务条款。
