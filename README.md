# wechat-telegram-bridge

在 Telegram 里收发微信消息：**每个微信会话自动对应一个 Telegram 群**。

- 微信收到新消息时，程序用你的 Telegram 账号自动建一个群（群名 = 微信备注名，群头像 = 微信头像），并把机器人拉进群。之后这个会话的消息都由机器人发到这个群里，所以你会收到正常的通知。
- 你在群里发的消息会发到对应的微信会话。
- 微信端依赖 [agent-wechat](https://github.com/thisnick/agent-wechat) 的守护进程（`wx serve`）和它的 HTTP API。

```
微信服务器 ⇄ agent-wechat (wx serve) ⇄ 本程序 ⇄ Telegram（你的账号 + 机器人）
```

## 支持的内容

| 微信 → Telegram | Telegram → 微信 |
|---|---|
| 文字、图片、视频、文件、表情 | 文字、图片、视频、文件 |
| 语音（装了 ffmpeg 会转成 Telegram 语音条，否则以音频文件发送） | 语音条（作为微信语音发送） |
| 链接卡片、位置（附地图点）、聊天记录、系统提示 | 贴纸：静态贴纸转成白底图片，动态/视频贴纸转成短视频（微信无法通过 agent-wechat 发表情） |
| 群聊消息显示发送人 | 回复某条消息 → 微信引用回复 |
| 引用回复（能对上的话变成 Telegram 回复） | 媒体的说明文字会作为一条单独的文字发送 |
| 撤回提示 | 以 `//` 开头的消息不发到微信 |
| 你在手机上发出的消息（静音同步，可以关） | |

不支持：以微信表情的形式发送贴纸（上游 agent-wechat 不支持发表情，图片会被转成 JPEG，所以透明背景变白、GIF 不动）、在 Telegram 删消息撤回微信消息、语音/视频通话。

## 部署（Docker Compose，一台 Linux VPS）

agent-wechat 和本程序跑在同一台 VPS 上。agent-wechat 的 API 只监听 `127.0.0.1:6174`，不对公网开放任何端口。需要 Docker 和 Docker Compose 插件（`docker compose version` 能输出版本号），x86_64 或 ARM64 Linux。

### 你需要自己准备的 3 样东西

1. **`api_id` 和 `api_hash`**：登录 https://my.telegram.org → *API development tools* 申请。
2. **`bot_token`**：找 [@BotFather](https://t.me/BotFather) 发 `/newbot` 创建机器人。

其余的（agent-wechat 的 token、微信账号、配置文件）都会自动生成。

### 安装

桥接程序的镜像由 GitHub Actions 自动构建（`.github/workflows/image.yml`，推送到 main 分支就会更新），发布在 `ghcr.io/speedupmaster/wechat-telegram-bridge`，支持 x86_64 和 ARM64。仓库是私有的，镜像也是私有的，所以 VPS 要先登录一次 ghcr.io：

1. 在 GitHub → Settings → Developer settings → Personal access tokens → *Tokens (classic)* 新建一个 token，只勾 `read:packages`。
2. 在 VPS 上执行（把 `TOKEN` 换成刚才的 token）：

```bash
echo TOKEN | docker login ghcr.io -u SpeedupMaster --password-stdin
```

然后把项目拿到 VPS 上：`git clone https://github.com/SpeedupMaster/wechat-telegram-bridge.git`（私有仓库，git 会要密码，填一个有 `repo` 权限的 token），或者用 `scp -r` 上传（`.venv`、`data` 不用传）。在目录里执行：

```bash
sh setup.sh
```

它会问你上面 3 个值（保存在 `.env`），然后拉取镜像并启动所有容器（拉不到桥接镜像时会改为在本机构建），最后显示一行：

```
    /login 3f9a1c07
```

不想用脚本也可以手动做：`cp .env.example .env`，填好 3 个值，`docker compose up -d`，再用 `docker compose logs telegram-bridge` 找 `PAIRING CODE`。

### 在机器人里完成登录

1. **配对**：在 Telegram 里打开你的机器人，发送上面那行 `/login 配对码`。配对码只出现在服务器日志里，所以别人就算找到你的机器人也没法操作。
2. **登录 Telegram**：程序需要登录你自己的 Telegram 账号来自动建群。机器人会让你选：
   - **扫码登录**：机器人发来一张二维码，用手机 Telegram 的「设置 → 设备 → 关联桌面设备」扫。二维码要显示在另一台设备上（比如在电脑版/网页版 Telegram 里打开和机器人的对话）。
   - **验证码登录**：发手机号，再把收到的验证码**用空格隔开**发给机器人（例如 `1 2 3 4 5`）。原样发送的话，Telegram 会认为验证码泄露并作废这次登录。
   - 开了两步验证的，按提示再发密码。密码和验证码消息机器人读完会立即删掉。
3. **登录微信**：Telegram 登录后，机器人会自动发来微信登录二维码，用手机微信「扫一扫」，然后在手机上确认。二维码过期会自动换新的。

看到「✅ 微信已登录」就完成了。之后微信来消息就会自动建群。登录状态保存在 Docker 卷里，重启、升级都不用再登。

> 如果在手机上确认登录后一直没反应：有些微信登录确认需要手机直连服务器的 TCP 8050-8100 端口。可以在 VPS 防火墙/安全组里临时放行这些端口，登录成功后再关掉。机器人等太久也会提示这一点。

### 日常维护

| 操作 | 命令 |
|---|---|
| 看日志 | `docker compose logs -f telegram-bridge` / `docker compose logs -f agent-wechat` |
| 重启 | `docker compose restart` |
| 停止 | `docker compose down`（数据保存在卷里，不会丢；不要加 `-v`） |
| 更新本程序 | 代码推送到 GitHub 后等 Actions 构建完，在 VPS 上 `docker compose pull telegram-bridge && docker compose up -d` |
| 固定某个版本 | 在 `.env` 里写 `BRIDGE_IMAGE=ghcr.io/speedupmaster/wechat-telegram-bridge:sha-xxxxxxx`（或打 `v1.0.0` 标签后用 `:1.0.0`） |
| 在 VPS 上自己构建 | `docker compose up -d --build`（需要完整源码） |
| 更新 agent-wechat | 在 `.env` 里写 `AGENT_WECHAT_VERSION=v2.x.x`，然后 `docker compose pull agent-wechat && docker compose up -d` |
| 微信掉线或被踢下线 | 机器人会通知你，私聊机器人发 `/wxlogin` 重新扫码 |
| 换一个 Telegram 账号 | `docker compose down`，删除 `bridge-data` 卷，再 `docker compose up -d`，重新配对 |
| 高级设置 | `cp config.example.toml config/config.toml`，编辑后 `docker compose restart telegram-bridge` |
| 备份 | 备份 `.env` 以及 `wechat-data`、`wechat-token`、`bridge-data` 三个卷（`wechat-config` 每次启动自动重写，不用备份） |

注意：

- 卷名前面会带上项目目录名，例如 `wechat-telegram-bridge_bridge-data`，可以用 `docker volume ls` 查看。
- `bridge-data` 卷里的 `user.session` 就是你的 Telegram 登录凭证；`wechat-token` 卷里的 token 能完全控制微信守护进程；`.env` 里的 bot token 能控制机器人。都要妥善保管。
- 程序重启时如果 Telegram 还没登录，会生成一个新的配对码，旧的作废。

## 本地运行（不用 Docker）

需要 Python 3.11 或更新版本，以及已经在运行的 agent-wechat 守护进程（安装见[官方文档](https://thisnick.github.io/agent-wechat/setup/install/)，`wx serve` 即可，微信不用提前登录）。

```bash
python -m venv .venv
.venv\Scripts\activate            # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
```

用环境变量（或 `config.toml`，见 `config.example.toml`）提供 `TG_API_ID`、`TG_API_HASH`、`TG_BOT_TOKEN`，以及守护进程的 token：`WECHAT_TOKEN`，或者 `WECHAT_TOKEN_FILE`（token 文件路径）。然后：

```bash
python -m bridge
```

终端会打印配对码，之后的登录步骤和上面一样，全部在机器人里完成。国内网络需要设置 `TG_PROXY`，例如 `socks5://127.0.0.1:7890`。建议装上 [ffmpeg](https://ffmpeg.org/)，这样微信语音会显示成 Telegram 语音条。

第一次启动只从“现在”开始转发，不会把以前的聊天记录灌进 Telegram。

## 使用

- 等微信来消息就会自动建群。想主动找某人聊：私聊你的机器人，发 `/find 张三` 搜索，再发 `/open wxid_xxx` 建群。
- 私聊机器人发 `/status` 查看运行状态（包括微信是否在线），`/help` 查看帮助，`/wxlogin` 重新登录微信。
- 微信掉线、被踢下线或恢复在线时，机器人会私聊通知你。
- 在桥接群里发 `/wx` 查看它对应的微信会话 ID，发 `/avatar` 重新获取微信头像作为群头像（建群时会自动设置，拿不到时可以手动再试）。
- 在 Telegram 里回复群里的某条消息，发到微信就是引用回复。
- 不想要某个群了：直接在 Telegram 删群。之后这个人再发消息时会建一个新群。
- 群多了以后，可以在 Telegram 里建一个“微信”聊天文件夹，把这些群放进去。

## 注意事项

- **封号风险**：agent-wechat 用的是非官方协议客户端，登录时算作“一台新设备”。微信可能对第三方客户端做风控，建议先用小号试，不要用来群发或营销。
- **Telegram 频率限制**：短时间建太多群会被要求等待（`FLOOD_WAIT`），程序会自动等。`create_interval_seconds` 控制两次建群的最小间隔。普通（非超级）群不占用 500 个超级群/频道的上限。
- **建的是 Telegram 普通群**。如果你手动把群升级成超级群，程序会自动跟着新 ID 走。
- `needs_review` 表示微信那边的发送结果不确定。程序会在群里提示，请在手机上确认，不要重复发送。
- 程序和 agent-wechat 最好跑在同一台机器上。agent-wechat 只监听 localhost，跨机器访问需要 HTTPS 或隧道。

## 文件说明

```
bridge/
  __main__.py   入口：python -m bridge [config.toml]
  config.py     读取环境变量和可选的 config.toml
  login.py      在机器人里登录：配对码、Telegram 扫码/验证码登录、微信二维码登录、掉线提醒
  wechat.py     agent-wechat HTTP API 客户端（账号、登录、SSE 事件流、发送、上传、下载媒体）
  core.py       桥接逻辑：建群、双向转发、引用、撤回、机器人命令
  store.py      SQLite：事件游标、微信会话 ↔ Telegram 群、消息 ID 对照
tests/          离线测试（模拟 agent-wechat 和 Telegram）
Dockerfile      桥接程序镜像（带 ffmpeg）
compose.yaml    初始化（token 和守护进程配置）+ agent-wechat + 桥接程序；只需要这一个文件就能部署
setup.sh        一键安装：写 .env、启动、显示配对码
.github/workflows/image.yml   GitHub Actions：跑测试，构建并推送镜像到 ghcr.io
.env.example    Telegram 的 3 个必填项
config/         可选的 config.toml 放这里（Docker）
```

运行测试：`python -m unittest discover -s tests -t . -v`

## 常见问题

- **机器人不理我**：Telegram 登录前，机器人只响应 `/login 配对码`；登录后只听你登录的那个 Telegram 账号的命令。
- **配对码找不到**：`docker compose logs telegram-bridge | grep "PAIRING CODE"`。容器重启会换新的配对码，用最新的。
- **配对码输错太多次**：连续错 10 次后机器人不再响应配对，`docker compose restart telegram-bridge` 换一个新码。
- **启动时报 “this token sees accounts”**：守护进程里有多个微信账号，用 `WECHAT_ACCOUNT` 或 `[wechat] account` 指定一个。
- **连不上 Telegram**：检查 `TG_PROXY`。
- **群里收不到消息**：私聊机器人发 `/status` 看微信是否在线，不在线就发 `/wxlogin`。再看 `docker compose logs telegram-bridge` 里每条微信消息对应的 `Forwarded` 或 `Skipped ...: 原因` 日志。程序只转发开始监听之后产生的消息。
- **想看详细日志**：在 compose.yaml 里给 telegram-bridge 加 `command: ["-v"]`，或本地运行 `python -m bridge -v`。
