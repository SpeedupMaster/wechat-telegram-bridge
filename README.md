# wechat-telegram-bridge

在 Telegram 中收发微信消息的双向桥接工具：**每个微信私聊/群聊自动对应一个 Telegram 群**。

- 微信收到新消息时，程序通过你的 Telegram 账号自动建群（群名 = 微信备注/群名，群头像 = 微信头像），并把你的 Bot 拉进群；后续消息由 Bot 发送到对应群中，保留正常的 Telegram 通知体验。
- 你在 Telegram 桥接群里发送的消息会实时转发到对应的微信会话。
- 微信侧依赖 [agent-wechat](https://github.com/thisnick/agent-wechat) 的本地守护进程（`wx serve`）及其 HTTP/SSE API。

```text
微信服务器 ⇄ agent-wechat (wx serve) ⇄ telegram-bridge ⇄ Telegram（你的账号 + 机器人）
```

---

## 功能特性

### 双向消息支持

| 微信 → Telegram | Telegram → 微信 |
|---|---|
| 文字消息（自动将 `#话题#` 转为 Telegram 可高亮 `#话题`，常用 `[捂脸]` 等微信表情转为 Emoji） | 文字消息 |
| 图片、视频、文件（优先拉取微信**原图**，支持 wxid/wxgf 头像解码） | 图片、视频、文件（默认以**原图**发送；在 Telegram 选择「**作为文件发送**」的图片也会绕过 Telegram 压缩并在微信端以**原图**接收，而非文件附件） |
| 表情：**静态/WXGF 预览表情转为 Telegram 静态贴纸（`.webp`），GIF 动图表情转为动态贴纸（`.webm`）** | **贴纸**：静态贴纸（`.webp`）、动态贴纸（`.tgs`/`.webm`）转为透明 GIF 直接发送为**微信原生表情**（兼容旧版 `agent-wechat` 时自动回退为图片/短视频） |
| 语音（容器自带 `ffmpeg` 时自动转为 Telegram 语音条） | 语音条（发送为微信语音） |
| 链接卡片、**视频号卡片**（自动清理低版本升级占位提示）、位置（含地图坐标）、聊天记录、系统提示 | |
| 微信群聊显示发送者名称 | **群内 `@` 提醒**：发送 `@成员名` 或 `@所有人` / `@all` 触发微信真实 `@` 提醒 |
| 引用回复对照还原为 Telegram 回复 | 回复某条消息 → 微信引用回复 |
| 撤回提示、手机端已发消息静音同步（可关闭） | **双向撤回**：在 Telegram 删除已发消息，或回复该消息发送 `/recall`，即可撤回微信消息（2 分钟内有效）；以 `//` 开头的内容只留在 Telegram |

### 文件大小与上游限制

- **不受 HTTP Bot API 20 MB / 50 MB 限制**：本桥接使用 Telethon 直连 Telegram **MTProto 协议**，Telegram 协议层单文件最高支持 2 GB。
- **默认媒体上限 `200 MB`**：由 `config.toml` 中的 `max_media_mb = 200` 控制（双向均检查）。由于收发媒体需先读入内存再转发，如需调大请确保服务器内存充足，同时微信侧本身也有文件大小上限。
- **原图清晰度**：依赖 `agent-wechat >= 2.4.0`，发送图片默认附带 `imageQuality = "original"`（JPEG 保留原字节，PNG/WebP/静态 GIF 按全分辨率转换），接收微信图片也会优先触发并下载 `original` 副本而非仅取已缓存的缩略图/标准图；在 Telegram 中以「**作为文件发送**」发送静态图片（≤ 25 MB、JPEG ≤ 8192 px / PNG 等 ≤ 4096 px，超出 4096 px 的非 JPEG 静态图会自动转为高质量 JPEG），微信端会以**原图**接收（动图 GIF 或超过 25 MB / 8192 px 的超大图片仍作为文件发送）。视频默认发送 720p 播放版及原始文件。
- **表情格式限制**：WXGF 格式的微信动图表情受限于上游 `agent-wechat` 仅解码单帧预览，转发到 Telegram 时会作为静态贴纸展示；无 CDN 下载地址的加密/特殊表情仍无法获取。
- **暂不支持**：微信语音/视频通话。

---

## 机器人命令

Bot 启动时会自动向 Telegram 注册斜杠 `/` 菜单（私聊和群聊菜单分开展示，无需手动去 `@BotFather` 配置）。

### 私聊机器人命令

| 命令 | 说明 |
|---|---|
| `/find 关键词` | 搜索微信联系人和微信群 |
| `/open 微信ID` | 为指定微信联系人或群聊新建对应的 Telegram 群（主动发起聊天） |
| `/chats [关键词]` | 查看当前已绑定的所有会话（别名 `/list`） |
| `/link 微信ID TG群ID` | 手动将已有的 Telegram 群绑定到指定微信会话 |
| `/status` | 查看运行时长、转发统计、已连接会话数与微信在线状态 |
| `/wxlogin` | 重新获取微信登录二维码（掉线或被踢下线时使用） |
| `/help` | 查看使用帮助 |

### 桥接群内命令

| 命令 | 说明 |
|---|---|
| `/wx` | 查看当前群对应的微信名称与会话 ID |
| `/members [关键词]` | 列出微信群成员（点击代码块即可复制 `@名字`，别名 `/at`） |
| `/recall` | 回复某条自己发的消息并发送此命令，撤回微信消息 |
| `/avatar` | 重新拉取微信头像并更新 Telegram 群头像 |
| `/rename` | 同步最新微信备注/群名到 Telegram 群标题（别名 `/title`） |
| `/unlink` | 解除当前 Telegram 群与微信会话的绑定 |
| `/help` | 查看群内可用命令帮助 |

---

## 部署指南（Docker Compose，Linux VPS）

`agent-wechat` 与 `telegram-bridge` 运行在同一台 Linux VPS 上（支持 x86_64 / ARM64），`agent-wechat` 仅监听 `127.0.0.1:6174`，不对外暴露端口。

### 1. 准备工作

开始前请准备以下 3 项凭证（其余 token、守护进程配置、数据库目录都会自动生成）：

1. **`TG_API_ID` 与 `TG_API_HASH`**：登录 <https://my.telegram.org> → *API development tools* 创建应用获取。
2. **`TG_BOT_TOKEN`**：在 Telegram 找 [@BotFather](https://t.me/BotFather) 发送 `/newbot` 创建机器人获取。

### 2. 安装与启动

GitHub Actions 会在推送到 `main` 分支后自动构建并发布多架构镜像到 `ghcr.io/speedupmaster/wechat-telegram-bridge`。

如果仓库/镜像为私有，先在 VPS 登录一次 GHCR（Token 只需勾选 `read:packages`）：

```bash
echo <GITHUB_PAT> | docker login ghcr.io -u SpeedupMaster --password-stdin
```

拉取代码并运行一键脚本：

```bash
git clone https://github.com/SpeedupMaster/wechat-telegram-bridge.git
cd wechat-telegram-bridge
sh setup.sh
```

脚本会引导填写 `.env` 中的必需变量、创建 `./data/` 持久化目录、拉取/构建镜像并启动容器，随后打印配对码：

```text
    /login 3f9a1c07
```

> 也可以手动部署：`cp .env.example .env` 并填写配置 → `docker compose up -d` → `docker compose logs telegram-bridge | grep "PAIRING CODE"` 查看配对码。

### 3. 在机器人内完成登录

1. **配对**：在 Telegram 私聊你的机器人，发送 `/login 配对码`（仅持有服务器日志的人可配对）。
2. **登录 Telegram 账号**：按机器人引导二选一（用于自动建群与收发账号状态）：
   - **扫码登录**：用手机 Telegram「设置 → 设备 → 关联桌面设备」扫描机器人发来的二维码（需在电脑端/网页版等另一块屏幕上展示）。
   - **验证码登录**：发送手机号，收到验证码后**用空格隔开**发送（如 `1 2 3 4 5`），避免被 Telegram 判定验证码泄露而作废；如开启两步验证再按提示输入密码。敏感消息会被机器人自动删除。
3. **登录微信**：Telegram 登录后机器人会自动发送微信登录二维码，用手机微信扫码确认即可。看到「✅ 微信已登录」即部署完成。

> **扫码确认卡住提示**：部分微信设备确认登录需要手机直连服务器 TCP `8050-8100` 端口；若扫码确认后久未响应，可临时在 VPS 安全组/防火墙放行该端口段，登录成功后再关闭。

---

## 日常维护与更新

### 只更新 `telegram-bridge`（保持微信不断线）

由于 [compose.yaml](compose.yaml) 中 `telegram-bridge` 对 `agent-wechat` 有 `depends_on` 依赖，更新桥接镜像时请务必加上 `--no-deps`，这样不会重启 `agent-wechat`：

- **拉取 GitHub Actions 构建好的最新镜像**：
  ```bash
  docker compose pull telegram-bridge
  docker compose up -d --no-deps telegram-bridge
  ```
- **或直接在服务器拉代码本地重新构建**：
  ```bash
  git pull
  docker compose up -d --no-deps --build telegram-bridge
  ```

### 其他常用操作

| 操作 | 命令 |
|---|---|
| 查看桥接日志 | `docker compose logs -f --tail 100 telegram-bridge` |
| 查看微信守护进程日志 | `docker compose logs -f agent-wechat` |
| 清理旧镜像 | `docker image prune -f` |
| 停止服务 | `docker compose down`（数据保存在 `./data/` 中不会丢失） |
| 指定桥接镜像版本 | 在 `.env` 中设置 `BRIDGE_IMAGE=ghcr.io/speedupmaster/wechat-telegram-bridge:sha-xxxxxxx` |
| 更新 `agent-wechat` | 在 `.env` 中修改 `AGENT_WECHAT_VERSION=v2.x.x`，再 `docker compose pull agent-wechat && docker compose up -d` |
| 高级配置 | `cp config.example.toml config/config.toml`，按需修改后执行 `docker compose restart telegram-bridge` |
| 更换 Telegram 账号 | `docker compose down`，删除 `data/bridge-data/`，重新 `docker compose up -d` 并配对 |

### 数据目录与备份

所有持久化状态均以 bind mount 保存在项目下的 `./data/` 目录中（已加入 `.gitignore`）：

- **建议定期备份**：`.env`、`config/`、`data/bridge-data`（含 Telegram `user.session` 与绑定数据库 `bridge.db`）、`data/wechat-data`（微信登录态）、`data/wechat-token`。
- **无需备份**：`data/wechat-config`（每次启动由 `token-init` 自动重写）、`data/wechat-scratch`（临时缓存）。

---

## 本地运行（不用 Docker）

要求 Python 3.11+、运行中的 `agent-wechat`（`wx serve`），推荐安装 [ffmpeg](https://ffmpeg.org/) 以支持语音条与动态贴纸转码。

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

通过环境变量（或 `config.toml`）提供 `TG_API_ID`、`TG_API_HASH`、`TG_BOT_TOKEN`、`WECHAT_TOKEN`（或 `WECHAT_TOKEN_FILE`），国内网络可加 `TG_PROXY=socks5://127.0.0.1:7890`，然后启动：

```bash
python -m bridge
```

运行全部单元测试：

```bash
python -m unittest discover -s tests -v
```

---

## 项目结构

```text
bridge/
  __main__.py   程序入口（python -m bridge [config.toml]）
  config.py     环境变量与可选 config.toml 配置加载
  login.py      Bot 内引导登录（配对码、Telegram 扫码/验证码登录、微信扫码、掉线告警）
  wechat.py     agent-wechat HTTP / SSE 客户端与群成员/媒体接口封装
  core.py       双向桥接主逻辑、视频号/话题/表情渲染、@提及解析、机器人命令与菜单注册
  stickers.py   Telegram 贴纸转微信图片/短视频（基于 Pillow / ffmpeg）
  store.py      SQLite 持久化（事件游标、会话映射、消息映射）
tests/          基于模拟客户端的离线单元测试
Dockerfile      桥接服务镜像构建文件（内置 ffmpeg）
compose.yaml    token-init + agent-wechat + telegram-bridge 编排文件
setup.sh        VPS 一键初始化脚本
config.example.toml  高级配置模板
```

---

## 注意事项与常见问题

- **微信风控提示**：`agent-wechat` 属于非官方协议客户端，登录时会显示为新设备，请勿用于高频群发或营销，建议先用测试账号验证。
- **Telegram 建群限流**：短时间批量新建群组会触发 `FLOOD_WAIT`，程序会自动等待；可通过 `config.toml` 中的 `create_interval_seconds`（默认 `20` 秒）控制间隔。默认创建的是普通群（不占用 500 个超级群上限），若你手动升级为超级群，桥接会自动迁移群 ID。
- **为什么只转发新消息**：首次启动会记录当前事件游标，不会把历史微信消息灌入 Telegram。
- **找不到配对码 / 配对失效**：执行 `docker compose logs telegram-bridge | grep "PAIRING CODE"`；容器重启会生成新配对码，连续输错 10 次将锁定，重启容器即可刷新。
- **提示 `needs_review`**：表示微信侧发送状态未确认，请在手机微信上核对是否发出，避免重复发送。
- **微信表情显示 `[表情，无法获取：…]`**：加密/部分自定义表情没有可下载 CDN 地址，属于 `agent-wechat` 协议限制；可以在日志中看到具体原因。
- **开启详细调试日志**：在 `compose.yaml` 的 `telegram-bridge` 下加 `command: ["-v"]`，或本地运行 `python -m bridge -v`。
