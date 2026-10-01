# FCFS Minter — Telegram Bot

Telegram control plane for the multi-chain FCFS minter (Base, Robinhood).

## Setup

1. **Create a bot** via [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token.

2. **Edit `bot/config.json`**:
   - `bot_token` — token from BotFather
   - `owner_id` — your Telegram user id (check [@userinfobot](https://t.me/userinfobot)). `0` = anyone, a number = you only
   - `private_key` — optional. Leave empty if keys are in `wallets.txt` (the minter auto-reads it)

3. **Run**:
   ```bash
   cd /home/ubuntu/fcfs-minter
   screen -S fcfs-bot
   .venv/bin/python bot/bot.py
   # Ctrl+A D to detach
   ```

4. Message the bot `/start`.

## Menu

- **Select chain** (Base / Robinhood) — becomes the default chain
- **⚡ Instant Mint** — slug → quantity → contract → fire immediately
- **📅 Schedule** — slug → quantity → contract → time (ISO/UTC) → bot auto-executes
- **📋 List Jobs** — view/delete scheduled jobs
- **💳 Wallet** — check balances across all chains

## Notes

- Long-polling via `requests` (bundled in the venv), no webhook.
- Each mint run is a subprocess to `minter.py` with an isolated temp env (`bot/run.env`, recreated per run).
- `bot/config.json`, `bot/jobs.json`, `bot/run.env` are git-ignored (never committed).
- Wallets are read from `wallets.txt` (repo root) or `private_key` in config.
