#!/usr/bin/env python3
"""
FCFS Minter Telegram Bot — multi-chain (base, robinhood)
Polling-only, no webhook. Uses `requests` (already in fcfs-minter venv).
Drives ../minter.py via subprocess with a temp env file.

Usage:
  python3 bot.py                 # read bot/config.json
  python3 bot.py --config X.json
"""
import json, os, re, subprocess, sys, threading, time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # fcfs-minter/
BOTDIR = Path(__file__).resolve().parent                # fcfs-minter/bot/
API = "https://api.telegram.org/bot{token}/{method}"

CHAIN_PRESETS = {
    "base": {
        "label": "Base 🟦",
        "rpc_url": "https://mainnet.base.org",
        "max_fee_gwei": 1.0,
        "max_priority_gwei": 0.05,
        "gas_limit": 300000,
        "gas_mode": "eip1559",
        "siwe_chain_id": 8453,
    },
    "robinhood": {
        "label": "Robinhood 🟨",
        "rpc_url": "https://rpc.mainnet.chain.robinhood.com",
        "max_fee_gwei": 0.1,
        "max_priority_gwei": 0.01,
        "gas_limit": 150000,
        "gas_mode": "legacy",
        "siwe_chain_id": 4663,
    },
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    """Tiny JSON store. One file for config, one for jobs."""

    def __init__(self, path: Path, default: dict):
        self.path = path
        self.default = default

    def load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except Exception:
            return json.loads(json.dumps(self.default))  # deep copy

    def save(self, data: dict) -> None:
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def api(token: str, method: str, **kw) -> dict:
    url = API.format(token=token, method=method)
    try:
        if method == "getUpdates":
            r = requests.get(url, params=kw, timeout=35)
        else:
            r = requests.post(url, json=kw, timeout=30)
        return r.json()
    except Exception as e:
        return {"ok": False, "description": str(e)}


class Bot:
    def __init__(self, cfg_path: Path):
        self.cfg_path = cfg_path
        self.store = Store(cfg_path, {"bot_token": "", "owner_id": 0})
        self.jobs_store = Store(BOTDIR / "jobs.json", {"jobs": []})
        self.cfg = self.store.load()
        self.token = self.cfg.get("bot_token", "")
        self.owner_id = int(self.cfg.get("owner_id") or 0)
        self.chain = "base"
        self.states = {}          # chat_id -> {flow, ...}
        self.jobs = self.jobs_store.load().get("jobs", [])
        self.lock = threading.Lock()
        self.stop = threading.Event()

    # ---------- persistence ----------
    def save_cfg(self):
        self.cfg["bot_token"] = self.token
        self.cfg["owner_id"] = self.owner_id
        self.store.save(self.cfg)

    def save_jobs(self):
        self.jobs_store.save({"jobs": self.jobs})

    # ---------- telegram ----------
    def call(self, method: str, **kw) -> dict:
        return api(self.token, method, **kw)

    def send(self, chat_id, text, keyboard=None, edit=False, msg_id=None):
        kw = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if keyboard:
            kw["reply_markup"] = keyboard
        if edit and msg_id:
            kw["message_id"] = msg_id
            r = self.call("editMessageText", **kw)
            # editMessageText fails silently on same-content or markup issues; fall back to sendMessage
            if not r.get("ok"):
                desc = r.get('description', '')
                if "is not modified" in desc:
                    return {"ok": True} # ignore
                print(f"[bot] editMsg fail: {desc[:120]}", flush=True)
                kw.pop("message_id", None)
                return self.call("sendMessage", **kw)
            return r
        return self.call("sendMessage", **kw)

    def kb(self, rows):
        return {"inline_keyboard": [[{"text": t, "callback_data": c} for t, c in row] for row in rows]}

    def answer_cb(self, cid, qid, text=None):
        kw = {"callback_query_id": qid}
        if text:
            kw["text"] = text
        self.call("answerCallbackQuery", **kw)

    # ---------- auth ----------
    def allowed(self, uid) -> bool:
        return self.owner_id == 0 or uid == self.owner_id

    # ---------- menu ----------
    def main_menu(self, chat_id, msg_id=None):
        kb = self.kb([
            [("🟦 Base", "setchain:base"), ("🟨 Robinhood", "setchain:robinhood")],
            [("⚡ Mint Instan", "mint"), ("📅 Schedule", "sched")],
            [("📋 List Jobs", "jobs"), ("💳 Wallet", "wallet")],
            [("🛑 Stop", "stop")],
        ])
        txt = (f"<b>FCFS Minter Bot</b>\n"
               f"Chain aktif: <b>{CHAIN_PRESETS[self.chain]['label']}</b>\n\n"
               f"Pilih aksi:")
        self.send(chat_id, txt, kb, edit=msg_id is not None, msg_id=msg_id)

    def chain_menu(self, chat_id, msg_id=None):
        kb = self.kb([
            [("🟦 Base", "setchain:base"), ("🟨 Robinhood", "setchain:robinhood")],
            [("⬅️ Kembali", "menu")],
        ])
        self.send(chat_id, "Pilih chain:", kb, edit=msg_id is not None, msg_id=msg_id)

    # ---------- mint flow ----------
    def start_mint(self, chat_id, msg_id=None):
        self.states[chat_id] = {"flow": "mint", "step": "slug"}
        self.send(chat_id, f"Chain: <b>{CHAIN_PRESETS[self.chain]['label']}</b>\n\n"
                           "Kirim <b>collection slug</b> (mis: robingangz):",
                  self.kb([[("⬅️ Back", "menu")]]),
                  edit=msg_id is not None, msg_id=msg_id)

    def start_sched(self, chat_id, msg_id=None):
        self.states[chat_id] = {"flow": "sched", "step": "slug"}
        self.send(chat_id, f"Chain: <b>{CHAIN_PRESETS[self.chain]['label']}</b>\n"
                           "Schedule mint.\n\nKirim <b>collection slug</b> (mis: robingangz):",
                  self.kb([[("⬅️ Back", "menu")]]),
                  edit=msg_id is not None, msg_id=msg_id)

    def handle_text(self, chat_id, text):
        st = self.states.get(chat_id)
        if not st:
            self.send(chat_id, "Gunakan menu /start")
            return
        flow, step = st.get("flow"), st.get("step")
        if flow in ("mint", "sched") and step == "slug":
            st["slug"] = text.strip().lower()
            st["step"] = "qty"
            self.send(chat_id, "Jumlah (quantity):", self.kb([[("⬅️ Back", "menu")]]))
        elif flow in ("mint", "sched") and step == "qty":
            qty = text.strip()
            if not qty.isdigit() or int(qty) < 1:
                self.send(chat_id, "Quantity harus angka ≥ 1. Coba lagi:", self.kb([[("⬅️ Back", "menu")]]))
                return
            st["qty"] = qty
            st["step"] = "contract"
            self.send(chat_id, "Contract address NFT (0x...):", self.kb([[("⬅️ Back", "menu")]]))
        elif flow in ("mint", "sched") and step == "contract":
            addr = text.strip().lower()
            if not re.fullmatch(r"0x[0-9a-f]{40}", addr):
                self.send(chat_id, "Format address salah (0x + 40 hex). Coba lagi:", self.kb([[("⬅️ Back", "menu")]]))
                return
            st["contract"] = addr
            if flow == "sched":
                st["step"] = "time"
                self.send(chat_id, "Jam eksekusi (ISO, UTC). Contoh:\n`2026-08-05T02:00:35+00:00`\natau\n`2026-08-05 09:00` (WIB)", self.kb([[("⬅️ Back", "menu")]]))
            else:
                st["step"] = "confirm"
                self.send(chat_id, self._summary(st) + "\n\nPilih Mode Eksekusi:\n<b>Normal</b>: Irit gas, via API\n<b>Spam</b>: Brutal bypass API (High Gas Risk)", self.kb([
                    [("✅ Normal", "mint:run:normal"), ("⚡ Spam (Brutal)", "mint:run:spam")],
                    [("❌ Batal", "menu")],
                ]))
        elif flow == "sched" and step == "time":
            ts = text.strip()
            # If plain format "YYYY-MM-DD HH:MM", assume WIB (+07:00)
            if len(ts) == 16 and "T" not in ts:
                ts = ts.replace(" ", "T") + ":00+07:00"
            try:
                from datetime import datetime
                import datetime as dt_mod
                dt = datetime.fromisoformat(ts)
                # Convert to UTC
                if dt.tzinfo is None:
                    # Assumed WIB (+07:00). Replace tzinfo to WIB
                    wib_tz = dt_mod.timezone(dt_mod.timedelta(hours=7))
                    dt = dt.replace(tzinfo=wib_tz)
                utc_dt = dt.astimezone(dt_mod.timezone.utc)
                st["time"] = utc_dt.isoformat()
            except Exception as e:
                self.send(chat_id, f"Format salah. Gunakan ISO (contoh: `2026-08-05T02:00:00+00:00`) atau lokal (`2026-08-05 09:00`).", self.kb([[("⬅️ Back", "menu")]]))
                return
            st["step"] = "confirm"
            self.send(chat_id, self._summary(st) + "\n\nPilih Mode Eksekusi:\n<b>Normal</b>: Irit gas, via API\n<b>Spam</b>: Brutal bypass API (High Gas Risk)", self.kb([
                [("✅ Normal", "sched:set:normal"), ("⚡ Spam (Brutal)", "sched:set:spam")],
                [("❌ Batal", "menu")],
            ]))
        elif flow == "wallet" and step == "pk":
            pk = text.strip()
            if not pk.startswith("0x"):
                pk = "0x" + pk
            if len(pk) != 66:
                self.send(chat_id, "Format PK salah (0x + 64 hex). Coba lagi:", self.kb([[("⬅️ Back", "wallet")]]))
                return
            # Add to wallets.txt
            wfile = ROOT / "wallets.txt"
            existing = wfile.read_text() if wfile.exists() else ""
            if pk not in existing:
                wfile.write_text((existing + "\n" + pk).strip() + "\n")
            self.states.pop(chat_id, None)
            self.send(chat_id, "✅ Private Key ditambahkan ke sistem.")
            self.wallet_info(chat_id)
        else:
            self.send(chat_id, "Langkah tidak dikenal. /start ulang.")

    def _summary(self, st):
        c = CHAIN_PRESETS[self.chain]
        return (f"<b>Konfirmasi</b>\n"
                f"Chain: {c['label']}\n"
                f"Slug: <code>{st['slug']}</code>\n"
                f"Qty: <b>{st['qty']}</b>\n"
                f"Contract: <code>{st['contract']}</code>\n\n"
                f"Mode: {'⚡ Instan' if st['flow'] == 'mint' else '📅 Schedule'}")

    def run_mint(self, chat_id, st):
        self.send(chat_id, "⏳ Menjalankan mint... (bisa 5-60 detik)")
        env = self.build_env(st)
        code, out = self.run_minter(env, at=None, mode=st.get("mode", "normal"))
        ok = code == 0
        # parse key lines for nice report
        lines = [l for l in out.splitlines() if l.startswith(("[tx]", "[done]", "[swap]", "[rpc-health]", "[receipt]", "[public]", "[presign]", "ERROR", "WARN"))]
        body = "\n".join(lines[-12:]) or out[-500:]
        self.send(chat_id, f"<b>{'✅ Sukses' if ok else '❌ Gagal'}</b> (exit {code})\n<pre>{body[:1800]}</pre>")

    def set_sched(self, chat_id, st):
        fire = st.get("time")
        if not fire:
            self.send(chat_id, "Waktu belum diset.")
            return
        job = {
            "id": f"j{int(time.time())}",
            "chain": self.chain,
            "slug": st["slug"],
            "qty": st["qty"],
            "contract": st["contract"],
            "at": fire,
            "mode": st.get("mode", "normal"),
            "status": "scheduled",
        }
        with self.lock:
            self.jobs.append(job)
            self.save_jobs()
        self.states.pop(chat_id, None)
        self.send(chat_id, f"✅ Job <code>{job['id']}</code> dijadwalkan:\n"
                           f"{CHAIN_PRESETS[job['chain']]['label']} · {job['slug']} ×{job['qty']}\n"
                           f"Eksekusi: <code>{fire}</code>", edit=True, msg_id=st.get("msg_id"))
        self.main_menu(chat_id)

    def handle_sched_time(self, chat_id, text):
        return False # Deprecated: logic moved to handle_text

    # ---------- env build + minter run ----------
    def build_env(self, st) -> str:
        c = CHAIN_PRESETS[self.chain]
        lines = [
            f"COLLECTION_SLUG={st['slug']}",
            f"NFT_CONTRACT={st['contract']}",
            f"CHAIN={self.chain}",
            f"QUANTITY={st['qty']}",
            "TOKEN_ID=0",
            f"RPC_URL={c['rpc_url']}",
            f"MAX_FEE_GWEI={c['max_fee_gwei']}",
            f"MAX_PRIORITY_GWEI={c['max_priority_gwei']}",
            f"GAS_LIMIT={c['gas_limit']}",
            f"SIWE_CHAIN_ID={c['siwe_chain_id']}",
            "LIVE=1",
            "DRY_FETCH=0",
            "AUTH_BASE=https://opensea.io/__api",
            "GQL_URL=https://gql.opensea.io/graphql",
            "SIWE_DOMAIN=opensea.io",
            "SIWE_URI=https://opensea.io/",
            "CONNECTOR_ID=injected",
            "WARMUP_SECONDS=5",
            "HAMMER_EARLY_SECONDS=3.0",
            "SCHEDULE_POLL_SECONDS=30",
            f"MODE={st.get('mode', 'normal')}"
        ]
        # wallet: use wallets.txt (minter auto-reads) unless PRIVATE_KEY set in config
        pk = self.cfg.get("private_key", "").strip()
        if pk:
            lines.insert(0, f"PRIVATE_KEY={pk}")
        return "\n".join(lines)

    def run_minter(self, env_text: str, at: str | None = None, mode: str = "normal") -> tuple[int, str]:
        envp = BOTDIR / "run.env"
        envp.write_text(env_text)
        if mode == "spam":
            cmd = [sys.executable, str(BOTDIR / "spam_runner.py"), str(envp)]
        else:
            cmd = [sys.executable, str(ROOT / "minter.py"), "run", "--env", str(envp)]
        
        if at and mode != "spam":
            cmd += ["--at", at]
            
        try:
            # If spam mode and at time is provided, we just sleep inside python using subprocess pre-delay wrapper
            if mode == "spam" and at:
                wrapper = f"import time, datetime, sys, subprocess; t=datetime.datetime.fromisoformat('{at}').timestamp(); d=t-time.time(); time.sleep(max(0,d)); sys.exit(subprocess.run({cmd}).returncode)"
                r = subprocess.run([sys.executable, "-c", wrapper], capture_output=True, text=True, timeout=180, cwd=str(ROOT))
            else:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=180, cwd=str(ROOT))
            return r.returncode, r.stdout + r.stderr
        except subprocess.TimeoutExpired:
            return 124, "TIMEOUT 180s"
        except Exception as e:
            return 2, str(e)

    # ---------- wallet ----------
    def wallet_info(self, chat_id, msg_id=None):
        try:
            from web3 import Web3
        except Exception as e:
            self.send(chat_id, f"web3 tidak terinstall: {e}", edit=msg_id is not None, msg_id=msg_id)
            return
        lines = []
        wfile = ROOT / "wallets.txt"
        keys = []
        if wfile.exists():
            keys = [l.strip() for l in wfile.read_text().splitlines() if l.strip() and not l.startswith("#")]
        pk = self.cfg.get("private_key", "").strip()
        if pk:
            keys.append(pk)
        seen = set()
        for k in keys:
            if not k.startswith("0x"):
                k = "0x" + k
            kl = k.lower()
            if kl in seen:
                continue
            seen.add(kl)
            try:
                acct = Web3().eth.account.from_key(kl)
                addr = acct.address
                for chain_name, c in CHAIN_PRESETS.items():
                    w3 = Web3(Web3.HTTPProvider(c["rpc_url"], request_kwargs={"timeout": 10}))
                    bal = w3.eth.get_balance(addr)
                    lines.append(f"• {chain_name}: {Web3.from_wei(bal, 'ether'):.6f} {chain_name.upper() if chain_name=='base' else 'ETH'}")
            except Exception as e:
                lines.append(f"• (invalid key: {e})")
        txt = "<b>Wallet</b>\n" + "\n".join(lines) if lines else "<b>Wallet</b>\n(kosong)"
        kb = self.kb([
            [("➕ Tambah PK", "wallet:add")],
            [("⬅️ Back", "menu")]
        ])
        self.send(chat_id, txt, kb, edit=msg_id is not None, msg_id=msg_id)

    def start_wallet_add(self, chat_id, msg_id=None):
        self.states[chat_id] = {"flow": "wallet", "step": "pk"}
        self.send(chat_id, "Kirim <b>Private Key</b> (0x...):", self.kb([[("⬅️ Back", "wallet")]]), edit=msg_id is not None, msg_id=msg_id)

    # ---------- jobs ----------
    def list_jobs(self, chat_id, msg_id=None):
        if not self.jobs:
            self.send(chat_id, "Belum ada job.", edit=msg_id is not None, msg_id=msg_id)
            return
        rows = []
        for j in self.jobs:
            lbl = CHAIN_PRESETS[j["chain"]]["label"]
            rows.append([(f"{lbl} {j['slug']} ×{j['qty']} @ {j['at']} [{j['status']}]",
                          f"job:del:{j['id']}")])
        rows.append([("⬅️ Kembali", "menu")])
        self.send(chat_id, "<b>Jobs:</b>", self.kb(rows), edit=msg_id is not None, msg_id=msg_id)

    def delete_job(self, chat_id, jid):
        with self.lock:
            before = len(self.jobs)
            self.jobs = [j for j in self.jobs if j["id"] != jid]
            if len(self.jobs) != before:
                self.save_jobs()
        self.send(chat_id, f"Job {jid} dihapus." if len(self.jobs) != before else f"Job {jid} tidak ada.")
    def scheduler_loop(self):
        import time
        from datetime import datetime
        while not self.stop.is_set():
            try:
                self.tick_jobs()
            except Exception as e:
                print(f"[sched] err {e}", flush=True)
            time.sleep(2)

    def tick_jobs(self):
        now = time.time()
        due = []
        with self.lock:
            for j in self.jobs:
                if j["status"] == "scheduled":
                    try:
                        t = datetime.fromisoformat(j["at"]).timestamp()
                    except Exception:
                        continue
                    # Start 30s before the scheduled time so the minter has setup + preflight headroom
                    if t - 30 <= now:
                        due.append(j)
            for j in due:
                j["status"] = "running"
            if due:
                self.save_jobs()
        for j in due:
            threading.Thread(target=self.execute_job, args=(j,), daemon=True).start()

    def execute_job(self, j):
        self.chain = j["chain"]
        st = {"slug": j["slug"], "qty": j["qty"], "contract": j["contract"]}
        self.send(self.owner_id, f"⏰ Job {j['id']} mulai persiapan: {CHAIN_PRESETS[j['chain']]['label']} {j['slug']} ×{j['qty']}")
        env = self.build_env(st)
        code, out = self.run_minter(env, at=j["at"], mode=j.get("mode", "normal"))
        ok = code == 0
        lines = [l for l in out.splitlines() if l.startswith(("[tx]", "[done]", "[swap]", "ERROR", "[rpc-health]", "[receipt]", "[public]", "[presign]", "WARN"))]
        body = "\n".join(lines[-15:]) or out[-800:]
        self.send(self.owner_id, f"<b>{'✅ Job sukses' if ok else '❌ Job gagal'}</b> {j['id']}\n<pre>{body[:1200]}</pre>")
        with self.lock:
            for jj in self.jobs:
                if jj["id"] == j["id"]:
                    jj["status"] = "done" if ok else "failed"
            self.save_jobs()

    # ---------- main loop ----------
    def handle_update(self, upd):
        if "message" in upd:
            m = upd["message"]
            cid = m["chat"]["id"]
            uid = m.get("from", {}).get("id", 0)
            if not self.allowed(uid):
                self.send(cid, "Akses ditolak.")
                return
            text = m.get("text", "")
            if text == "/start" or text == "/menu":
                self.main_menu(cid)
                return
            if text.startswith("/"):
                return
            if self.handle_sched_time(cid, text):
                return
            self.handle_text(cid, text)
        elif "callback_query" in upd:
            cb = upd["callback_query"]
            cid = cb["message"]["chat"]["id"]
            qid = cb["id"]
            uid = cb.get("from", {}).get("id", 0)
            if not self.allowed(uid):
                self.answer_cb(cid, qid, "Akses ditolak")
                return
            data = cb["data"]
            msg_id = cb["message"]["message_id"]
            self.answer_cb(cid, qid)
            try:
                if data == "menu":
                    self.main_menu(cid, msg_id)
                elif data.startswith("chain:"):
                    self.chain = data.split(":", 1)[1]
                    self.main_menu(cid, msg_id)
                elif data.startswith("setchain:"):
                    self.chain = data.split(":", 1)[1]
                    self.main_menu(cid, msg_id)
                elif data == "mint":
                    self.start_mint(cid, msg_id)
                elif data == "sched":
                    self.start_sched(cid, msg_id)
                elif data.startswith("mint:run:"):
                    mode = data.split(":", 2)[2]
                    st = self.states.get(cid)
                    if st:
                        st["mode"] = mode
                        self.run_mint(cid, st)
                        self.states.pop(cid, None)
                elif data.startswith("mint:cancel"):
                    self.states.pop(cid, None)
                    self.main_menu(cid, msg_id)
                elif data.startswith("sched:set:"):
                    mode = data.split(":", 2)[2]
                    st = self.states.get(cid)
                    if st:
                        st["mode"] = mode
                        self.set_sched(cid, st)
                elif data == "jobs":
                    self.list_jobs(cid, msg_id)
                elif data.startswith("job:del:"):
                    jid = data.split(":", 2)[2]
                    self.delete_job(cid, jid)
                    self.list_jobs(cid, msg_id)
                elif data == "wallet":
                    self.wallet_info(cid, msg_id)
                elif data == "wallet:add":
                    self.start_wallet_add(cid, msg_id)
                elif data == "stop":
                    self.send(cid, "Bot dihentikan. Restart: jalankan ulang bot.py")
                    self.stop.set()
                else:
                    print(f"[bot] unhandled callback data: {data}")
            except Exception as e:
                print(f"[bot] callback err {e}")
                self.send(cid, f"Error: {e}")

    def run(self):
        if not self.token:
            print("ERROR: bot_token kosong. Isi di bot/config.json", file=sys.stderr)
            return 2
        # init owner_id if unset
        if not self.owner_id:
            me = self.call("getMe")
            print(f"[bot] @{me.get('result', {}).get('username', '?')}", flush=True)
        th = threading.Thread(target=self.scheduler_loop, daemon=True)
        th.start()
        offset = 0
        print("[bot] polling started", flush=True)
        while not self.stop.is_set():
            try:
                r = self.call("getUpdates", offset=offset, timeout=25)
                if r.get("ok"):
                    for upd in r.get("result", []):
                        offset = upd["update_id"] + 1
                        try:
                            self.handle_update(upd)
                        except Exception as e:
                            print(f"[bot] handle err {e}", flush=True)
                else:
                    time.sleep(3)
            except Exception as e:
                print(f"[bot] poll err {e}", flush=True)
                time.sleep(3)
        print("[bot] stopped", flush=True)
        return 0


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--config", default=str(BOTDIR / "config.json"))
    args = p.parse_args()
    bot = Bot(Path(args.config))
    # first-run: auto-register owner via /start
    # token might be empty; print instructions
    if not bot.token:
        print("Bot token belum diisi.")
        print(f"Edit {args.config}: set 'bot_token' dan 'owner_id' (dari @userinfobot).")
        return 1
    raise SystemExit(bot.run())


if __name__ == "__main__":
    import requests
    main()
