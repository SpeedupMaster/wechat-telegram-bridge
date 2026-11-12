可选：把 `config.example.toml` 复制到这里，命名为 `config.toml`，用来调整高级设置（群名模板、是否桥接群聊/公众号等）。

Telegram 的 api_id / api_hash / bot_token 写在项目根目录的 `.env` 里，不需要写在这里。
改完后执行 `docker compose restart telegram-bridge` 生效。
