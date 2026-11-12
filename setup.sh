#!/usr/bin/env sh
# One-step setup on the VPS: asks for the three Telegram values, starts everything,
# and prints the pairing code to send to your bot.
set -eu
cd "$(dirname "$0")"

if ! docker compose version >/dev/null 2>&1; then
  echo "需要 Docker 和 Docker Compose 插件（docker compose version 能输出版本号）。" >&2
  exit 1
fi

ask() {  # ask VAR "prompt"
  value=""
  while [ -z "$value" ]; do
    printf '%s' "$2"
    read -r value
  done
  eval "$1=\$value"
}

if [ ! -f .env ]; then
  echo "== 填写 Telegram 信息（只需一次，保存在 .env）=="
  echo "api_id / api_hash：在 https://my.telegram.org → API development tools 申请"
  ask TG_API_ID "TG_API_ID: "
  ask TG_API_HASH "TG_API_HASH: "
  echo "bot_token：找 @BotFather 发 /newbot 创建机器人得到"
  ask TG_BOT_TOKEN "TG_BOT_TOKEN: "
  printf 'TG_PROXY（海外 VPS 直接回车跳过）: '
  read -r TG_PROXY || TG_PROXY=""
  (
    umask 077
    {
      echo "TG_API_ID=$TG_API_ID"
      echo "TG_API_HASH=$TG_API_HASH"
      echo "TG_BOT_TOKEN=$TG_BOT_TOKEN"
      echo "TG_PROXY=$TG_PROXY"
    } > .env
  )
  echo "已写入 .env"
fi

mkdir -p config
if docker compose pull; then
  docker compose up -d
else
  echo "拉取 telegram-bridge 镜像失败（私有镜像需要先 docker login ghcr.io，见 README）。改为在本机构建…"
  docker compose pull --ignore-buildable
  docker compose up -d --build
fi

echo "等待桥接程序启动…"
i=0
while [ $i -lt 90 ]; do
  logs=$(docker compose logs --no-log-prefix telegram-bridge 2>/dev/null || true)
  code=$(printf '%s\n' "$logs" | sed -n 's/.*PAIRING CODE: \([0-9a-f]*\).*/\1/p' | tail -n 1)
  if [ -n "$code" ]; then
    echo
    echo "在 Telegram 里打开你的机器人，发送："
    echo
    echo "    /login $code"
    echo
    echo "然后按机器人的提示登录 Telegram、扫码登录微信。"
    exit 0
  fi
  if printf '%s\n' "$logs" | grep -q "Telegram user:"; then
    echo "Telegram 已经登录过了。如果微信还没登录，机器人会把二维码发给你（也可以私聊机器人发 /wxlogin）。"
    exit 0
  fi
  i=$((i + 1))
  sleep 2
done
echo "没等到桥接程序的输出，看看日志：docker compose logs telegram-bridge" >&2
exit 1
