# wechat-telegram-bridge

基于 [agent-wechat](https://github.com/thisnick/agent-wechat) 与 [Telethon](https://github.com/LonamiWebs/Telethon)（MTProto 协议）构建的 **微信 ↔ Telegram 双向桥接服务**。

它将每个微信私聊或群聊自动映射为独立的 **Telegram 群组**（或同一个超级群内的**独立话题**），让你在 Telegram 中获得接近原生的微信收发、引用回复、原生表情互通、群内 `@` 提醒、消息撤回以及语音通话接听与拨打体验。

```text
微信服务器 ⇄ agent-wechat (127.0.0.1:6174) ⇄ telegram-bridge (MTProto) ⇄ Telegram（你的账号 + Bot）
                                                    │
                                     (可选) CallWeb (127.0.0.1:8780)
                                                    ⇅
                                     HTTPS 反代 / Cloudflare Tunnel ⇄ Telegram Mini App 语音通话
```

---

## 目录

- [核心特性](#核心特性)
- [运行模式：群组模式 vs 话题模式](#运行模式群组模式-vs-话题模式)
- [快速开始（Docker Compose）](#快速开始docker-compose)
- [日常使用与机器人命令](#日常使用与机器人命令)
- [在 Telegram 接听与拨打微信语音通话（可选）](#在-telegram-接听与拨打微信语音通话可选)
- [配置说明](#配置说明)
- [运维与数据管理](#运维与数据管理)
- [本地运行与开发](#本地运行与开发)
- [常见问题 (FAQ)](#常见问题-faq)

---

## 核心特性

### 1. 两种会话组织模式
- **群组模式（`groups`，默认）**：每个微信会话自动创建一个 Telegram 群（群名 = 微信备注/群名，群头像 = 微信头像），拥有独立的通知、置顶与归档体验。
- **话题模式（`topics`）**：所有微信会话集中在同一个开启了「话题 (Topics)」的 Telegram 群内，每个会话对应一个独立话题，**只需机器人即可运行，无需登录个人 Telegram 账号**。
- **无损切换**：两种模式的绑定关系在数据库中分开保存，随时修改 `BRIDGE_MODE` 切换不会丢失已有绑定。

### 2. 全类型消息双向互通
- **文字与富文本**：自动将微信 `#话题#` 转换为 Telegram 可点击高亮标签，将 `[捂脸]`、`[破涕为笑]` 等百余种微信内置表情转换为对应 Emoji。
- **原图、视频与大文件直传**：
  - **突破 Bot API 体积限制**：直连 Telegram MTProto 协议（协议层最高支持 2 GB，桥接默认限制 `max_media_mb = 200`）。
  - **流式下载与断点续传**：微信附件采用流式分块下载（`≤ 8 MiB` 留存内存，超出部分自动落盘至临时文件并支持 HTTP `Range` 断点续传，转发后自动清理），传输大视频/大文件不会撑爆内存。
  - **双向原图清晰度**：微信端收发图片默认拉取并发送 `original` 原图；在 Telegram 选择「**作为文件发送**」的静态图片（≤ 25 MB）也会自动以**微信原图**形式送达，而非变成文件附件。
- **原生表情包互通**：
  - **微信 → Telegram**：静态表情及 WXGF 预览自动转为 Telegram 静态贴纸（`.webp`），GIF 动图表情自动转为 Telegram 动态贴纸（`.webm`）。
  - **Telegram → 微信**：静态贴纸（`.webp`）、动态贴纸（`.tgs` / `.webm`）以及 GIF 动图自动转换为透明通道 GIF 并作为**微信原生表情**发送（静态最高 512 px、动态最高 384 px，内置转换缓存）。
- **语音条自动转码**：容器内置 `ffmpeg`，微信语音自动转为 Telegram 原生语音条（OGG/Opus），Telegram 发送的语音条直接作为微信语音发出。
- **分享卡片与特色消息**：
  - **横向图文链接卡片**：公众号、小红书、哔哩哔哩、知乎、GitHub 等分享链接自动渲染为微信风格的紧凑图文卡片，附带平台品牌标识与直达按钮（自动剔除冗长追踪参数）。
  - **丰富消息类型**：支持视频号卡片（含视频自动下载）、地理位置（渲染为 Telegram 原生 Venue 地标卡片）、合并转发聊天记录、微信系统提示等。

### 3. 聊天交互深度对齐
- **双向引用回复**：在 Telegram 中回复某条消息，微信端即显示为对该消息的引用回复，反之亦然。
- **群聊真实 `@` 提醒**：在 Telegram 群内输入 `@成员名` 或 `@所有人` / `@all`，自动解析并触发微信群内的原生 `@` 强提醒；配合 `/members` 命令可一键点按复制群成员 `@` 标签。
- **双向撤回**：手机端撤回消息实时同步提示；在 Telegram 中**删除单条已发消息**或回复消息发送 `/recall`，即可撤回 2 分钟内的微信消息（批量删除或清空历史不会误触撤回）。
- **多端静音同步与已读联动**：在手机微信发出的消息会自动静音同步至对应 Telegram 会话（标记为 `我（手机端）`）；在 Telegram 回复消息后自动将对应微信会话标为已读。
- **Telegram 内部备注**：以 `//` 开头的消息仅保留在 Telegram 群/话题内，不会转发到微信。

### 4. 微信来电通知与实时语音通话
- **实时来电提醒与拒接**：微信单聊语音/视频来电、群语音通话邀请实时推送到对应会话，支持点击「**📵 拒接**」一键挂断。
- **在 Telegram 内接听语音（可选）**：配置 HTTPS 域名（支持内置 Cloudflare Tunnel 免公网端口或 Caddy/Nginx 反代）后，可点击「**📞 接听**」或「**📞 加入**」在 Telegram Mini App 网页中使用麦克风直接通话，支持静音与挂断。
- **从 Telegram 打微信电话（可选，`agent-wechat >= 2.7.0`）**：在单聊对应的群/话题里发送 `/call`，点「**📞 开始呼叫**」打开同一个通话页面拨出，响铃中可随时取消，接通后与接听来电体验一致；在微信群对应的群/话题里发送 `/call`，勾选要邀请的成员即可发起**群语音通话**。

### 5. 微信好友管理（`agent-wechat >= 2.7.0`）
- **好友请求推送**：新的微信好友请求以卡片推送到机器人私聊，展示验证消息与历史留言，一键「**✅ 通过**」「**💬 通过并聊天**」（同时建好群/话题）或「**🔒 通过（仅聊天）**」；`/requests` 随时查看待处理请求。
- **主动添加好友**：私聊发送 `/add 微信号 [验证消息]`，先搜索预览，确认后再发送好友请求。
- **好友资料与朋友圈权限**：在会话里发送 `/contact`，查看微信号、昵称、备注与好友关系，点按切换「仅聊天 / 不让 TA 看我的朋友圈 / 不看 TA 的朋友圈」，或删除好友（聊天记录与绑定保留）。
- **名称自动同步**：微信备注或群名变化后，自动更新对应 Telegram 群/话题标题。

---

## 运行模式：群组模式 vs 话题模式

通过 `.env` 中的 `BRIDGE_MODE`（或 `config.toml` 中的 `bridge.mode`）切换：

| 对比维度 | `groups`（群组模式，默认） | `topics`（话题模式） |
| :--- | :--- | :--- |
| **展现形式** | 每个微信私聊/群聊对应一个独立的 Telegram 群 | 所有会话集中在同一个话题群内，每个微信会话对应一个话题 |
| **所需凭证** | Telegram Bot + **登录你的个人 Telegram 账号**（用于自动建群） | **仅需 Telegram Bot**（无需登录个人 Telegram 账号） |
| **头像与标识** | 自动将微信好友/群聊头像同步为 Telegram 群头像 | 话题不支持自定义头像，使用话题图标颜色区分（蓝色 = 私聊，绿色 = 群聊） |
| **使用体验** | 最接近原生聊天软件，可独立设置每个会话的通知、置顶与归档 | 聊天列表集中在单群内，列表清爽，但会话间切换需进入话题列表 |
| **平台限制** | 短时间大量建群受 Telegram `FLOOD_WAIT` 频率限制（内置间隔保护） | 几乎无建群风控风险；需将 Bot 设为话题群管理员并授予「管理话题」权限 |

> 修改 `.env` 中的 `BRIDGE_MODE` 后，执行 `docker compose up -d --no-deps telegram-bridge` 即可切换生效。

---

## 快速开始（Docker Compose）

本方案将 `agent-wechat` 守护进程与 `telegram-bridge` 部署在同一台 Linux VPS 上（支持 `x86_64` 与 `ARM64`）。容器采用 Host 网络模式，`agent-wechat` 仅监听本机回环地址 `127.0.0.1:6174`，不向公网暴露任何端口。

### 第一步：准备 Telegram 凭证

部署前只需准备以下 3 项参数（其余守护进程配置、API Token 与数据库目录均由容器在启动时自动初始化）：

1. **`TG_API_ID` 与 `TG_API_HASH`**：登录 [https://my.telegram.org](https://my.telegram.org) → 进入 *API development tools* 创建应用获取。
2. **`TG_BOT_TOKEN`**：在 Telegram 中私聊 [@BotFather](https://t.me/BotFather) 发送 `/newbot` 创建机器人获取。

### 第二步：一键初始化与启动

如果使用私有 GHCR 镜像，请先在 VPS 上登录一次容器镜像仓库（Personal Access Token 仅需勾选 `read:packages` 权限；若未登录，脚本会自动回退为本地构建镜像）：

```bash
echo <GITHUB_PAT> | docker login ghcr.io -u SpeedupMaster --password-stdin
```

克隆仓库并运行一键引导脚本：

```bash
git clone https://github.com/SpeedupMaster/wechat-telegram-bridge.git
cd wechat-telegram-bridge
sh setup.sh
```

`setup.sh` 会交互式引导你填写 `.env` 配置、创建 `./data/` 持久化目录、启动全部服务，并在终端打印一次性配对码：

```text
在 Telegram 里打开你的机器人，发送：

    /login 3f9a1c07
```

<details>
<summary><b>💡 手动部署方式（不使用 setup.sh）</b></summary>

```bash
cp .env.example .env
# 编辑 .env 填入 TG_API_ID、TG_API_HASH、TG_BOT_TOKEN
docker compose up -d
# 从日志中获取配对码
docker compose logs telegram-bridge | grep "PAIRING CODE"
```
</details>

### 第三步：在机器人内完成配对与登录

根据你选择的 `BRIDGE_MODE`，在 Telegram 中私聊你的机器人完成后续步骤：

#### 方式 A：群组模式（`BRIDGE_MODE=groups`，默认）

1. **安全配对**：向机器人发送 `/login 配对码`（仅能查看服务器日志的人可完成配对）。
2. **登录个人 Telegram 账号**（二选一，可随时点击按钮切换）：
   - **💬 验证码登录（单设备推荐）**：
     1. 点击底部「**📱 一键使用当前手机号**」按钮授权号码（或手动输入带国家码的手机号，如 `+8613800000000`）。
     2. 收到验证码后，直接在机器人消息下方的**内联数字键盘**上逐位点按并点击「**✅ 确认登录**」——全程不通过聊天框发送明文验证码，防止被 Telegram 判定为验证码泄露而强制注销。
     3. 若开启了两步验证（2FA），按提示发送密码（机器人验证后会立即自动删掉你的密码消息）。
   - **📷 扫码登录**：适合已在电脑版或网页版打开该对话的场景，使用手机 Telegram「设置 → 设备 → 关联桌面设备」扫描机器人发出的二维码。
3. **登录微信**：
   - Telegram 登录成功后，机器人会自动请求微信登录。
   - 若此前已登录过，会优先向手机微信推送**免扫码确认请求**，在手机上点击「登录」即可（也可点击「📷 改用扫码登录」）；首次登录则会直接将微信二维码图片发到对话中。
   - 看到「**✅ 微信已登录**」即代表部署完成！后续若微信意外掉线，机器人推送的通知中也会自带一键重登按钮。

#### 方式 B：话题模式（`BRIDGE_MODE=topics`）

1. **安全配对**：向机器人发送 `/login 配对码`。话题模式无需登录个人 Telegram 账号，发送配对码的用户即自动成为桥接主人（若从群组模式切换而来，会自动沿用原账号，无需重新配对）。
2. **配置话题群**：
   1. 新建一个 Telegram 群组，在群设置中开启「**话题 (Topics)**」。
   2. 将你的机器人拉入群中并设为**管理员**，确保勾选「**管理话题 (Manage Topics)**」权限（机器人必须是管理员才能接收群内普通消息并创建话题）。
   3. 在该群内发送 `/forum`（或在 `.env` 中设置 `TG_FORUM_CHAT=-100...` 固定）。
3. **登录微信**：同上，按机器人提示在手机确认或扫码登录微信。

> [!TIP]
> **手机点确认后卡住？**
> 部分微信环境在进行设备确认登录时，需要手机客户端直连服务器的 TCP `8050-8100` 端口。若在手机微信点击「登录」后久未响应（约 90 秒后机器人也会自动提示），请在 VPS 防火墙/安全组中临时放行 TCP `8050-8100` 端口，登录成功后再关闭即可。

---

## 日常使用与机器人命令

### 实用聊天技巧

- **引用回复**：在 Telegram 中长按/右键**回复**某条消息，发到微信即为引用回复。
- **群内 `@` 提醒**：在群内发送 `@群成员名称` 或 `@所有人` / `@all` 即可触发微信原生 `@` 提醒；不确定成员名字时可发 `/members` 点按复制。
- **撤回微信消息**：在消息发出 **2 分钟内**，直接在 Telegram **删除该单条消息**，或回复该消息发送 `/recall`，即可同步撤回微信端的消息。
- **发送不进微信的备注**：以 `//` 开头的消息（例如 `// 稍后跟进`）只会留在 Telegram 群/话题中，不会转发到微信。
- **发送无损原图**：直接发图或勾选「**作为文件发送**」的静态图片（≤ 25 MB），到达微信端均会作为高清原图展示。

### 机器人命令速查

机器人启动时会自动向 Telegram 注册斜杠 `/` 菜单（私聊与群聊菜单独立展示，无需手动前往 `@BotFather` 配置）。

#### 私聊机器人命令

| 命令 | 说明 |
| :--- | :--- |
| `/find [关键词]` | 搜索微信联系人与微信群（不填关键词则列出最近会话，可点击按钮直接创建群/话题；同群成员等非好友会标注「非好友」） |
| `/open [关键词]` | 为指定微信联系人或群聊新建 Telegram 群/话题并主动发起会话 |
| `/chats [关键词]` | 查看当前已绑定的所有微信会话列表（别名 `/list`） |
| `/link [关键词]` | 交互式点选绑定现有的 Telegram 群与微信会话（仅 `groups` 模式；也支持 `/link 微信ID TG群ID`） |
| `/requests` | 查看待处理的微信好友请求，点名字展开卡片后一键通过；没有待处理时显示等待对方通过的请求 |
| `/add 微信号 [验证消息]` | 按微信号搜索微信用户，预览确认后发送好友请求（可选「仅聊天」）；已是好友时直接给出发起聊天按钮 |
| `/status` | 查看桥接运行时长、收发统计、已绑定会话数、微信在线状态与待处理好友请求数 |
| `/wxlogin` | 手动触发微信重新登录（支持手机确认或扫码） |
| `/help` | 查看完整使用指南 |

#### 桥接群 / 话题内命令

| 命令 | 适用模式 | 说明 |
| :--- | :---: | :--- |
| `/wx` | 通用 | 查看当前群/话题绑定的微信名称与原始会话 ID |
| `/contact` | 通用 | 查看微信好友资料（微信号、昵称、备注、好友关系），点按切换朋友圈权限或删除好友（别名 `/info`） |
| `/call [成员]` | 通用 | 单聊里给好友拨打微信语音电话；微信群里弹出成员选择器发起群语音通话，可带名字预先勾选（如 `/call 张三 李四`）。需配置下文的通话页面 |
| `/members [关键词]` | 通用 | 列出当前微信群成员，点击代码块即可复制 `@名字`（别名 `/at`） |
| `/recall` | 通用 | 回复自己发出的某条消息并发送此命令，撤回对应微信消息（2 分钟内有效） |
| `/rename` | 通用 | 重新拉取最新微信备注名/群名并更新到当前群/话题标题（别名 `/title`） |
| `/link [关键词]` | 通用 | 将当前群或当前话题绑定（或改绑）到指定的微信会话 |
| `/unlink` | 通用 | 解除当前群或当前话题与微信会话的绑定 |
| `/avatar` | 仅 `groups` | 重新拉取微信头像并同步为当前 Telegram 群头像 |
| `/forum` | 仅 `topics` | 将当前开启了话题的超级群设为桥接主群 |
| `/help` | 通用 | 查看群内命令帮助 |

> **话题模式补充说明**：在话题群的 `General` 主话题中发送的普通聊天消息不会转发到微信；若某个子话题被手动删除，当该微信会话收到下一条消息时会自动重建新话题。

---

## 在 Telegram 接听与拨打微信语音通话（可选）

默认情况下，收到微信语音/视频来电或群语音邀请时，对应群/话题会收到实时通知并提供「**📵 拒接**」按钮。

如果进一步为通话服务（默认仅监听本机 `127.0.0.1:8780`）配置一个公网 **HTTPS** 地址，来电通知下方会额外出现「**📞 接听**」（单聊语音）或「**📞 加入**」（群语音）按钮，点开即可通过 Telegram Mini App 网页使用手机/电脑麦克风与微信端实时通话。

配置好通话页面后，也可以**主动给微信好友打电话**：在单聊对应的群/话题里发送 `/call`，机器人会回复一张呼叫卡片，点「**📞 开始呼叫**」打开通话页面，再点「呼叫」并授权麦克风后才会真正拨出。对方响铃时页面和卡片上都可以取消；对方接听后与接听来电的体验完全一致（静音、挂断、20 秒内重新进入）。呼叫卡片 10 分钟内未使用会自动失效，也可以点「✖️ 取消」作废。

在**微信群**对应的群/话题里发送 `/call` 则会发起**群语音通话**：微信要求发起时指定被邀请的成员，所以机器人会回复一张成员选择卡片，点按成员勾选（每页 10 人，可翻页、全选、清空，一次最多 100 人），选好后点「**📞 开始呼叫（N 人）**」。也可以在命令后带上名字、备注或微信号预先勾选，例如 `/call 张三 李四`。发起后卡片会显示邀请名单、已加入人数以及被拒绝或需要验证的邀请；挂断只代表你离开群通话，其他人可以继续。接通后，群通话卡片上会出现「**➕ 邀请更多人**」：点开会回复一张同样的成员选择卡片（已在通话中或邀请仍在等待的成员不会列出，被拒绝或需要验证的可以重新邀请），勾选后点「**➕ 发送邀请（N 人）**」即可，结果会更新到通话卡片上（需要 agent-wechat 报告 `conferenceInvite` 能力，且只适用于从 Telegram 发起的群通话）。桥接会从你在手机上发出的消息中自动识别你自己的微信号，并把你从成员列表中排除（刚部署、还没发过消息时，列表里可能会出现你自己，不要勾选即可）。

> 由于浏览器安全策略仅允许在 **HTTPS** 页面下调用麦克风，请选择以下**方案一**或**方案二**配置 HTTPS 接入：

### 方案一：使用内置 Cloudflare Tunnel（免开公网端口、免配证书，推荐）

只需拥有一个托管在 Cloudflare 的域名，VPS 无需开放任何入站端口，也无需安装 Caddy/Nginx：

1. 登录 Cloudflare 控制台 → 进入 **Zero Trust → Networks → Tunnels** → 点击 **Create a tunnel**，类型选择 **Cloudflared**，命名后复制安装命令中的 Token（以 `eyJ...` 开头的长字符串）。
2. 在该隧道的 **Public Hostname** 标签页添加一条记录：
   - **Subdomain**：例如 `call`
   - **Domain**：选择你的域名（如 `example.com`）
   - **Service Type**：选择 `HTTP`，**URL** 填写 `127.0.0.1:8780`
3. 在项目的 `.env` 文件中添加：
   ```ini
   CALL_WEB_URL=https://call.example.com
   COMPOSE_PROFILES=cloudflare
   CF_TUNNEL_TOKEN=eyJ...
   ```
4. 启动并应用配置：
   ```bash
   docker compose up -d cloudflared
   docker compose up -d --no-deps telegram-bridge
   ```

> [!WARNING]
> 请勿对该子域名启用 Cloudflare Access 访问鉴权、Bot Fight Mode 或交互式质询页面，否则会导致 Mini App 无法加载或音频 WebSocket 连接失败；也不要使用每次重启都会变更域名的 `trycloudflare.com` 临时隧道。

### 方案二：使用自建反向代理（Caddy / Nginx）

1. 将域名（如 `call.example.com`）解析至 VPS 公网 IP，并在防火墙放行 `80` / `443` 端口。
2. 配置反向代理转发至 `127.0.0.1:8780`：
   - **Caddy 配置示例**（自动签发证书并原生支持 WebSocket）：
     ```caddyfile
     call.example.com {
         reverse_proxy 127.0.0.1:8780
     }
     ```
   - **Nginx 注意事项**：需显式开启 WebSocket 升级头（`proxy_http_version 1.1; proxy_set_header Upgrade $http_upgrade; proxy_set_header Connection "upgrade";`）并将 `proxy_read_timeout` 调大至 `3600s`。支持挂载在子路径下（如 `https://example.com/wxcall`，转发时需保留路径前缀）。
3. 在 `.env` 中设置 `CALL_WEB_URL=https://call.example.com`，并执行 `docker compose up -d --no-deps telegram-bridge`。

### 注册为 Telegram Mini App（推荐）

配置好 `CALL_WEB_URL` 后，推荐在 [@BotFather](https://t.me/BotFather) 处注册 Mini App，以获得最佳的内嵌打开体验与双重身份校验：

1. 私聊 `@BotFather` 发送 `/newapp`，选择你的机器人，按提示填写标题与简介，**Web App URL** 填入你的 `CALL_WEB_URL`（如 `https://call.example.com`），最后设置一个短名称（short name，例如 `call`）。
2. 在 `.env` 中填入该短名称并重启桥接容器：
   ```ini
   CALL_WEB_APP=call
   ```
   ```bash
   docker compose up -d --no-deps telegram-bridge
   ```
- **注册后**：「📞 接听」按钮会直接在 Telegram 内以原生 Mini App 弹出，并额外通过 Telegram 签名校验访问者是否为桥接主人本人。
- **不注册时**：「📞 接听」按钮作为普通链接在内置浏览器中打开，依靠链接内的一次性高强度随机令牌进行鉴权。

### 通话使用说明与限制

- **麦克风权限**：首次接听时需授权 Telegram 或浏览器使用麦克风；若部分客户端内置 WebView 不支持麦克风采集，页面会自动提示「在浏览器中打开」。
- **前后台保持**：建议佩戴耳机以避免外放回声。通话期间请保持页面处于前台；若意外关闭页面，**20 秒内**通过来电消息上的「🎧 通话界面」按钮重新进入即可无缝恢复通话，超过 20 秒未连接页面则自动挂断（也可直接点击消息下方的「📴 挂断」按钮）。
- **上游限制**：目前仅支持**语音通话**（视频来电仅支持通知与一键拒接），从 Telegram 主动发起的单聊呼叫在对方约 90 秒未接听时会自动取消；群通话需要 agent-wechat 报告 `conferenceCreate` 能力，通话中追加邀请需要 `conferenceInvite`，加入别人发起的群通话时无法追加邀请；受上游 `agent-wechat` 限制，单次通话最长持续 20 分钟。

---

## 配置说明

本项目的配置分为两层：**环境变量（`.env`）** 与可选的 **高级配置文件（`config/config.toml`）**。同名配置项下，环境变量优先级高于 `config.toml`。

### 环境变量（`.env`）

| 变量名 | 必填 | 默认值 | 说明 |
| :--- | :---: | :--- | :--- |
| `TG_API_ID` | 是 | — | Telegram API ID（在 `my.telegram.org` 申请） |
| `TG_API_HASH` | 是 | — | Telegram API Hash |
| `TG_BOT_TOKEN` | 是 | — | Telegram 机器人 Token（在 `@BotFather` 申请） |
| `TG_PROXY` | 否 | 空 | 连接 Telegram 的代理地址，如 `socks5://127.0.0.1:7890`（海外 VPS 留空） |
| `BRIDGE_MODE` | 否 | `groups` | 运行模式：`groups`（一会话一群）或 `topics`（单群多话题） |
| `TG_FORUM_CHAT` | 否 | `0` | `topics` 模式下的话题群 ID（`-100...`），留空可在群内发 `/forum` 指定 |
| `CALL_WEB_URL` | 否 | 空 | 微信语音接听页面的公网 HTTPS 地址，留空则关闭网页接听 |
| `CALL_WEB_LISTEN` | 否 | `127.0.0.1:8780` | 语音接听页面在容器/宿主机本机监听的地址与端口 |
| `CALL_WEB_APP` | 否 | 空 | `@BotFather` `/newapp` 注册的 Mini App 短名称 |
| `COMPOSE_PROFILES` | 否 | 空 | 设为 `cloudflare` 时随 Compose 启动内置的 `cloudflared` 隧道容器 |
| `CF_TUNNEL_TOKEN` | 否 | 空 | Cloudflare Tunnel 的隧道 Token |
| `AGENT_WECHAT_VERSION` | 否 | `v2.7.1` | 上游 `agent-wechat` 镜像版本标签 |

### 高级配置（`config/config.toml`）

如需微调桥接行为，可将模板复制为 `config/config.toml` 并按需修改，修改后执行 `docker compose restart telegram-bridge` 生效：

```bash
cp config.example.toml config/config.toml
```

| 配置项 (`[bridge]`) | 默认值 | 说明 |
| :--- | :--- | :--- |
| `title_direct` | `"{name}"` | 微信私聊对应的群名/话题名模板（`{name}` 为微信备注或昵称） |
| `title_group` | `"👥 {name}"` | 微信群聊对应的群名/话题名模板 |
| `bridge_groups` | `true` | 是否桥接微信群聊消息 |
| `bridge_official_accounts` | `false` | 是否桥接微信公众号/服务号推送 |
| `forward_outgoing` | `true` | 是否将你在手机/电脑微信上发出的消息同步到 Telegram |
| `outgoing_silent` | `true` | 同步手机端自发消息时是否静音发送（不触发 Telegram 通知） |
| `sync_avatar` | `true` | 自动建群时是否将微信头像设为 Telegram 群头像（仅 `groups` 模式有效） |
| `mark_read_on_reply` | `true` | 在 Telegram 回复消息后，是否自动将该微信会话标记为已读 |
| `create_interval_seconds` | `20` | 两次自动建群之间的最小间隔秒数，防止触发 Telegram 限流 |
| `max_media_mb` | `200` | 单个媒体文件的收发体积上限（MB） |
| `call_notifications` | `true` | 是否推送微信语音/视频来电通知与一键拒接按钮（关闭后也无法接听或用 `/call` 拨打） |
| `link_card_images` | `true` | 是否将微信分享链接渲染为横向图文卡片 |
| `friend_requests` | `true` | 是否把新的微信好友请求推送到机器人私聊（需 `agent-wechat >= 2.7.0`） |
| `auto_rename` | `true` | 微信备注/群名变化后是否自动更新 Telegram 群/话题标题（关闭后可发 `/rename` 手动同步） |
| `gif_as_sticker` | `true` | 是否将 Telegram 发送的 GIF 动图转为微信原生表情发送 |
| `skip_threads` | `["weixin"]` | 忽略不桥接的微信会话 ID 列表（如 `["filehelper", "weixin"]`） |

---

## 运维与数据管理

### 无感更新 `telegram-bridge`（保持微信不断线）

在 [compose.yaml](compose.yaml) 中，`telegram-bridge` 声明了对 `agent-wechat` 的启动依赖。日常更新桥接程序时，**请务必带上 `--no-deps` 参数**，这样只会重建桥接容器，不会重启 `agent-wechat`，从而保证微信登录态与长连接完全不中断：

- **拉取预构建的最新镜像更新**：
  ```bash
  docker compose pull telegram-bridge
  docker compose up -d --no-deps telegram-bridge
  ```
- **或者拉取最新源码在本地重新构建**：
  ```bash
  git pull
  docker compose up -d --no-deps --build telegram-bridge
  ```

### 常用运维命令

| 操作场景 | 执行命令 |
| :--- | :--- |
| 查看桥接实时日志 | `docker compose logs -f --tail 100 telegram-bridge` |
| 查看微信守护进程日志 | `docker compose logs -f --tail 100 agent-wechat` |
| 停止所有服务 | `docker compose down`（持久化数据保存在 `./data/` 中不会丢失） |
| 升级 `agent-wechat` 版本 | 修改 `.env` 中的 `AGENT_WECHAT_VERSION` 后执行 `docker compose pull agent-wechat && docker compose up -d` |
| 更换绑定的 Telegram 账号 | `docker compose down` → `rm -rf data/bridge-data/` → `docker compose up -d` 并重新配对 |
| 清理无用旧镜像 | `docker image prune -f` |

### 持久化目录与备份建议

所有运行状态均通过 Bind Mount 保存在项目根目录下的 `./data/` 中（已加入 `.gitignore`）：

| 路径 | 建议备份 | 内容说明 |
| :--- | :---: | :--- |
| `.env` 与 `config/` | **是** | 环境变量密钥与自定义高级配置 |
| `data/bridge-data/` | **是** | SQLite 数据库 `bridge.db`（会话绑定、话题映射、消息 ID 映射、事件游标）及 Telegram 登录会话 `user.session` |
| `data/wechat-data/` | **是** | `agent-wechat` 持久化的微信设备信息与登录态 |
| `data/wechat-token/` | **是** | 容器间通信的守护进程 API Token |
| `data/wechat-config/` | 否 | 每次启动时由 `token-init` 自动生成的守护进程配置文件 |
| `data/wechat-scratch/` | 否 | 微信媒体与表情处理的临时缓存目录 |

---

## 本地运行与开发

如果不使用 Docker，也可以直接在宿主机运行 `telegram-bridge`（需自行启动 `agent-wechat` 的 `wx serve` 守护进程）。

### 环境要求
- **Python 3.11+**
- 运行中的 `agent-wechat`（默认监听 `http://127.0.0.1:6174`）
- 推荐安装系统级依赖：`ffmpeg` 与 `gifsicle`（用于语音条转码与动态贴纸转换优化）、中文字体与 Emoji 字体（如 `fonts-noto-cjk`、`fonts-noto-color-emoji`，用于渲染分享卡片）

### 启动与测试

```bash
# 1. 创建虚拟环境并安装依赖
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 2. 配置环境变量（或复制 config.example.toml 为 config.toml 填写）
export TG_API_ID=123456
export TG_API_HASH="your_api_hash"
export TG_BOT_TOKEN="123456:ABC-DEF..."
export WECHAT_TOKEN="your_agent_wechat_token"

# 3. 启动桥接服务（加 -v 可输出详细调试日志）
python -m bridge

# 4. 运行离线单元测试套件
python -m unittest discover -s tests -v
```

### 项目结构

```text
.
├── bridge/
│   ├── __main__.py        # CLI 入口（python -m bridge [config.toml] [-v]）
│   ├── config.py          # 环境变量与 config.toml 配置解析及校验
│   ├── contacts.py        # 微信好友请求卡片、/requests、/add、/contact 好友资料与朋友圈权限
│   ├── core.py            # 双向桥接核心逻辑、消息转换、@提及解析、命令与菜单注册
│   ├── login.py           # Bot 交互式引导配对、Telegram 内联键盘/扫码登录、微信重登逻辑
│   ├── wechat.py          # agent-wechat HTTP/SSE 异步客户端、流式媒体下载与断点续传
│   ├── callweb.py         # Telegram Mini App 微信语音通话页面与实时 PCM 音频 WebSocket 中转
│   ├── cards.py           # 微信链接分享横向图文卡片渲染与平台品牌识别（基于 Pillow）
│   ├── stickers.py        # Telegram 贴纸与微信原生表情双向转换及缓存（基于 Pillow / ffmpeg / rlottie）
│   └── store.py           # SQLite 状态存储（事件游标、群组/话题映射、双向消息 ID 映射）
├── tests/                 # 基于 Mock 客户端的完整离线单元测试
├── compose.yaml           # Docker Compose 编排（token-init + agent-wechat + telegram-bridge + cloudflared）
├── Dockerfile             # 桥接服务镜像构建文件（内置 ffmpeg、gifsicle 与 Noto CJK/Emoji 字体）
├── setup.sh               # VPS 一键交互式初始化与启动脚本
├── .env.example           # 环境变量模板
└── config.example.toml    # 高级配置模板
```

---

## 常见问题 (FAQ)

<details>
<summary><b>1. 微信账号有什么安全注意事项？</b></summary>

`agent-wechat` 属于非官方协议客户端，首次登录在微信设备管理中会显示为新设备登录。建议仅用于个人日常消息收发，切勿用于高频群发、自动拉群或营销行为；初次使用可先用小号验证稳定性。
</details>

<details>
<summary><b>2. 为什么首次启动后没有同步之前的微信历史消息？</b></summary>

为了避免首次启动时将成百上千条旧消息瞬间灌入 Telegram 触发建群限流，桥接在初次连接时会记录当前最新的事件游标（Cursor），仅从启动时刻起转发新收到的消息。若你想主动找某个尚未发来新消息的联系人聊天，私聊机器人发送 `/find` 或 `/open` 即可。
</details>

<details>
<summary><b>3. 自动建群会触发 Telegram 频率限制或达到群上限吗？</b></summary>

- **频率限制**：短时间内新建大量群组可能触发 Telegram 的 `FLOOD_WAIT`，程序内置了自动等待重试以及 `create_interval_seconds = 20`（默认每 20 秒最多建一个群）的限速保护。
- **群数量上限**：桥接在 `groups` 模式下默认创建的是 Telegram **普通群（Basic Group）**，不占用账号 500 个超级群/频道的上限；如果你手动将某个普通群升级为超级群（Supergroup），桥接会自动捕获迁移事件并无缝更新数据库中的群 ID。
- 如果你的微信群非常多、不想创建大量群组，推荐将 `BRIDGE_MODE` 设为 `topics` 使用话题模式。
</details>

<details>
<summary><b>4. 找不到配对码，或者配对码输错多次被锁定怎么办？</b></summary>

- 查看当前配对码：`docker compose logs telegram-bridge | grep "PAIRING CODE"`
- 为防止暴力枚举，配对码连续输错 10 次后将自动锁定。执行 `docker compose restart telegram-bridge` 重启容器即可生成新的配对码。
</details>

<details>
<summary><b>5. 发送消息后机器人提示 <code>needs_review</code> 是什么意思？</b></summary>

表示 `agent-wechat` 守护进程已提交该发送请求，但未在预期时间内收到微信服务器的明确回执。为防止自动重试导致在微信里重复发送，桥接会提示你打开手机微信核对该条消息是否已发出。
</details>

<details>
<summary><b>6. 为什么个别微信表情显示为静态图或提示 <code>[表情，无法获取：…]</code>？</b></summary>

- **WXGF 动图表情**：受限于上游 `agent-wechat` 目前仅对 WXGF 编码表情提供单帧预览解码，这类表情转发到 Telegram 时会作为高清静态贴纸展示（普通 GIF 微信表情则为动态贴纸）。
- **无法获取的表情**：极少数商城加密表情或特殊自定义表情在微信消息体中不携带可下载的 CDN 链接，属于上游协议限制。
- **企业微信发来的图片/文件**：企业微信联系人（`@openim`）发来的图片和文件不使用普通微信的 CDN 字段，上游 `agent-wechat` 目前无法下载（[thisnick/agent-wechat#467](https://github.com/thisnick/agent-wechat/issues/467)），桥接会显示 `[图片，无法获取：企业微信发来的图片/文件…]` 并附上文件名，请在手机微信查看原文件。
</details>

<details>
<summary><b>7. 如何开启 Debug 级别的详细日志？</b></summary>

在 `compose.yaml` 的 `telegram-bridge` 服务下添加 `command: ["-v"]` 并执行 `docker compose up -d --no-deps telegram-bridge`，或本地运行时使用 `python -m bridge -v`。
</details>
