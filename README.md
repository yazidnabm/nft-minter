# fcfs-minter

High-frequency FCFS NFT minting bot for OpenSea drops — multi-chain (Base, Robinhood, Ethereum), multi-wallet, with a Telegram control plane.

> **Default is `LIVE=0`.** Nothing broadcasts on-chain until you explicitly opt in. Use burner keys.

---

## Table of contents

1. [What it does](#what-it-does)
2. [Architecture](#architecture)
3. [Why it's fast](#why-its-fast)
4. [Directory layout](#directory-layout)
5. [Setup](#setup)
6. [Configuration](#configuration)
7. [CLI usage](#cli-usage)
8. [Telegram bot](#telegram-bot)
9. [Execution paths](#execution-paths)
10. [Safety & security](#safety--security)

---

## What it does

Mints NFTs the instant a drop goes live. Two execution paths:

| Path | Trigger | Needs OpenSea? | Speed |
|------|---------|----------------|-------|
| **Public bypass** | Public sale stage detected | No — reads SeaDrop contract on-chain directly | Fastest |
| **Allowlist / FCFS** | Allowlist stage (signature-protected) | Yes — OpenSea GraphQL for signed calldata | Fast |

Auth is [SIWE](https://eips.ethereum.org/EIPS/eip-4361) (Sign-In with Ethereum) against OpenSea's internal API, then either minting path depending on the active drop phase.

## Architecture

```
┌─────────────┐     ┌──────────────────┐     ┌──────────────────┐
│  Telegram    │────▶│   bot/bot.py     │────▶│   minter.py      │
│  control     │     │  (polling ctrl)  │     │  (mint engine)   │
└─────────────┘     └──────────────────┘     └────────┬─────────┘
                                                      │
                          ┌───────────────────────────┼───────────────────────────┐
                          ▼                           ▼                           ▼
                   ┌─────────────┐            ┌─────────────┐            ┌─────────────┐
                   │ OpenSea GQL │            │ SeaDrop      │            │ RPC blast   │
                   │ (allowlist) │            │ (public)     │            │ (multi-RPC) │
                   └─────────────┘            └─────────────┘            └─────────────┘
```

- **`minter.py`** — the engine. SIWE auth, drop schedule/eligibility, public bypass, pre-signing, multi-RPC blast, receipt verification.
- **`bot/bot.py`** — Telegram long-polling controller. Spawns `minter.py` as a subprocess with an isolated env per run.
- **`bot/spam_runner.py`** — raw contract-firing runner for non-OpenSea drops (brutal mode).
- **`bot/start.sh`** — wrapper that runs the bot under `screen` so it survives SSH disconnects.

## Why it's fast

The FCFS race is won or lost in the **fire moment** — everything before it is pre-computed.

1. **Pre-sign at T−2s.** Calldata is fetched, transactions are signed, and the raw bytes are staged *before* the drop opens. At fire time the only remaining work is dispatch.
2. **Fire-and-forget blast.** Transactions go to all RPCs simultaneously via detached threads. The caller returns in ~6 ms instead of blocking on network round-trips (measured: 265 ms → 6 ms, a 44× reduction).
3. **Persistent keep-alive sockets.** RPC connections are warmed pre-drop and reused, so the fire moment pays zero TLS handshake cost.
4. **Spin-wait timing.** Coarse `sleep` until 100 ms out, then a tight spin loop for sub-millisecond precision on the fire timestamp.
5. **Negative offset.** Optional `EARLY_FIRE_MS` fires slightly before the block boundary so the transaction is already in the mempool when the contract accepts it (critical on FIFO chains).
6. **FIFO gas strategy.** On first-in-first-out chains (Robinhood), priority fee is set to `0` — bribing validators is useless when ordering is by arrival.

## Directory layout

```
fcfs-minter/
├── minter.py              # Mint engine (auth, routing, pre-sign, blast, receipt)
├── requirements.txt
├── .env.example           # Template — copy to .env
├── wallets.txt.example    # Template — copy to wallets.txt
├── README.md              # This file
└── bot/
    ├── bot.py             # Telegram controller
    ├── spam_runner.py     # Raw contract fire runner
    ├── start.sh           # screen wrapper
    ├── test_bot.py        # Offline unit checks
    └── README.md          # Bot-specific docs
```

## Setup

```bash
cd fcfs-minter
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
cp wallets.txt.example wallets.txt
# edit .env (or wallets.txt) with your burner key + target drop
```

### Verify before going live

```bash
.venv/bin/python minter.py selfcheck   # offline unit checks
.venv/bin/python minter.py auth        # SIWE login only
.venv/bin/python minter.py schedule    # fetch drop schedule/eligibility
.venv/bin/python minter.py fetch       # fetch calldata, do NOT broadcast
```

## Configuration

All configuration lives in `.env`. Required vs optional:

### Required

| Key | Example | Notes |
|-----|---------|-------|
| `PRIVATE_KEY` *or* `wallets.txt` | `0x…` (64 hex) | Burner key. `wallets.txt` = one key per line |
| `COLLECTION_SLUG` | `my-drop` | From `opensea.io/collection/<slug>` |
| `NFT_CONTRACT` | `0x…` | NFT contract address |
| `CHAIN` | `base` \| `robinhood` | Must match the drop's chain |
| `RPC_URL` | `https://mainnet.base.org` | Primary RPC |

### Optional (have defaults)

| Key | Default | Purpose |
|-----|---------|---------|
| `QUANTITY` | `1` | Mints per wallet |
| `TOKEN_ID` | `0` | ERC-1155 id; ERC-721 uses `0` |
| `MAX_FEE_GWEI` | `1.0` | Fixed gas cap (never estimate in the hot path) |
| `MAX_PRIORITY_GWEI` | `0.05` | Priority fee (use `0` on FIFO chains) |
| `GAS_LIMIT` | `300000` | Gas limit |
| `WARMUP_SECONDS` | `5` | Pre-drop warm-up window |
| `HAMMER_EARLY_SECONDS` | `1.5` | When to start hammering for calldata |
| `SCHEDULE_POLL_SECONDS` | `30` | Schedule refresh interval |
| `FORCE_MINT_AT` | *(empty)* | Override mint time (ISO-8601 UTC) |
| `LIVE` | `0` | `1` = broadcast on-chain |
| `DRY_FET` | *(unset)* | Fetch calldata even when `LIVE=0` |
| `SIWE_CHAIN_ID` | `8453` | EVM chain id for SIWE (Base=8453, ETH=1, Robinhood=4663) |
| `EARLY_FIRE_MS` | `0` | Negative-offset early fire |
| `RPC_URLS` | *(optional)* | Comma-separated extra RPCs for blast redundancy |

## CLI usage

```bash
.venv/bin/python minter.py <stage> [--env PATH] [--at ISO8601] [--mode normal|spam]
```

| Stage | What it does |
|-------|--------------|
| `selfcheck` | Offline unit checks (no network) |
| `auth` | SIWE login, dump session |
| `schedule` | Fetch drop schedule + eligibility |
| `warmup` | Warm nonces + RPC connections |
| `fetch` | Fetch swap calldata, no broadcast |
| `run` | Full pipeline (dry unless `LIVE=1`) |

## Telegram bot

See [`bot/README.md`](bot/README.md).

Quick start:

```bash
# 1. create a bot via @BotFather, note the token
# 2. edit bot/config.json  (token, owner_id, optional private_key)
# 3. launch
screen -S fcfs-bot
.venv/bin/python bot/bot.py
# Ctrl+A D to detach
```

The bot drives the same engine through a menu: pick chain → instant mint or schedule → wallet/balance checks → job list.

## Execution paths

### Public bypass (fastest)

When a public sale stage is active, the bot reads the SeaDrop contract directly (`getPublicDrop`, `getAllowedFeeRecipients`) and calls `mintPublic` — no OpenSea round-trip, no signature.

### Allowlist / FCFS

Signature-protected drops need OpenSea's signed calldata. The bot hits OpenSea GraphQL at T−2s, pre-signs, then fires at the drop time.

> **Note:** the "spam/brutal" mode only works for standard ERC-721 public mints. SeaDrop drops with signature-protected stages will silently fail without the server-produced proof — use the allowlist path for those.

## Safety & security

- **`LIVE=0` by default.** Nothing is broadcast unless you set `LIVE=1`.
- **Never commit secrets.** `.env` and `wallets.txt` are git-ignored. `.env.example` / `wallets.txt.example` are templates only.
- **Burner keys only.** Never paste private keys into chat, logs, or commits.
- **OpenSea's internal API changes without notice.** Treat the GraphQL/auth flow as brittle; verify with `selfcheck` + `fetch` before every drop.
- **Colocate near the drop's sequencer** (e.g. `us-east-1` for Base) if you're racing a real FCFS drop.
