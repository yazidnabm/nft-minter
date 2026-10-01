#!/usr/bin/env python3
"""
OpenSea FCFS NFT minter — multi-chain (Base, Robinhood, Ethereum).

Flow:
  SIWE -> cookies -> dropBySlug schedule -> warm-up nonces
  -> hammer swap(action:MINT) (aliased multi-wallet) -> sign -> send

Public mint bypass (SeaDrop without signature):
  fetch_public_drop() -> build_public_mint() -> pre-sign -> blast_to_all()

Default LIVE=0 (no broadcast). OpenSea internal API can change anytime.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import socket
import ssl
import http.client
import concurrent.futures
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit
import urllib.request

import requests
from requests.adapters import HTTPAdapter
from requests.exceptions import RequestException
from dotenv import load_dotenv
from eth_account import Account
from eth_account.messages import encode_defunct
from web3 import Web3

ROOT = Path(__file__).resolve().parent
ZERO = "0x" + "0" * 40
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# SeaDrop addresses per chain
SEADROP_ADDRESSES = {
    1: "0x00005EA00Ac477B1030CE78506496e8C2dE24bf5",      # ETH
    8453: "0x00005EA00Ac477B1030CE78506496e8C2dE24bf5",   # Base
    4663: "0x00005EA00Ac477B1030CE78506496e8C2dE24bf5",   # Robinhood
}

OPENSEA_FEE_RECIPIENT = "0x0000a26b00c1F0DF003000390027140000fAa719"

# Minimal selection — OpenSea GraphQL field sets change often.
# If server rejects unknown fields, tighten via --swap-fields or env.
SWAP_SELECTION = """
{
  actions {
    __typename
    ... on TransactionAction {
      transactionSubmissionData {
        to
        data
        value
        chain { identifier }
      }
    }
  }
  errors {
    __typename
    message
  }
}
""".strip()

DROP_SCHEDULE_QUERY = """
query DropSchedule($slug: String!) {
  dropBySlug(slug: $slug) {
    __typename
    stages {
      stageIndex
      stageType
      label
      startTime
      endTime
    }
  }
}
"""

DROP_ELIGIBILITY_QUERY = """
query DropEligibilityQuery($collectionSlug: String!, $address: Address!) {
  dropBySlug(slug: $collectionSlug) {
    __typename
    ... on Erc721SeaDropV1 {
      minterQuantityMinted(minter: $address)
    }
    stages {
      stageType
      stageIndex
      isEligible
      maxTotalMintableByWallet
      eligibleMaxTotalMintableByWallet
      eligiblePrice {
        token {
          unit
          symbol
          contractAddress
          chain { identifier }
        }
      }
    }
  }
}
"""


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _now() -> float:
    return time.time()


def explorer_link(chain: str, tx_hash: str) -> str:
    """Return block explorer URL for a tx hash on a given chain."""
    if chain == "robinhood":
        return f"https://explorer.robinhood.com/tx/{tx_hash}"
    if chain == "base":
        return f"https://basescan.org/tx/{tx_hash}"
    return f"https://etherscan.io/tx/{tx_hash}"


def _iso_to_ts(s: str) -> float:
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s).timestamp()


@dataclass
class Wallet:
    key: str
    address: str
    account: Any
    nonce: int | None = None
    access_token: str | None = None
    cookies: dict = field(default_factory=dict)


@dataclass
class Config:
    private_keys: list[str]
    collection_slug: str
    nft_contract: str
    chain: str
    quantity: str
    token_id: str
    rpc_url: str
    rpc_urls: list[str]  # NEW: multiple RPC endpoints for blast
    early_fire_ms: int  # NEW: fire early by this many ms
    max_fee_gwei: float
    max_priority_gwei: float
    gas_limit: int
    warmup_s: float
    hammer_early_s: float
    schedule_poll_s: float
    force_mint_at: str | None
    live: bool
    dry_fetch: bool
    auth_base: str
    gql_url: str
    siwe_domain: str
    siwe_uri: str
    siwe_chain_id: int
    connector_id: str

    @classmethod
    def load(cls, env_path: Path | None = None) -> "Config":
        if env_path:
            # --env must win over process env / prior dotenv
            load_dotenv(env_path, override=True)
        else:
            load_dotenv(ROOT / ".env")

        keys: list[str] = []
        single = (os.getenv("PRIVATE_KEY") or "").strip()
        if single:
            keys.append(single)
        multi = (os.getenv("PRIVATE_KEYS") or "").strip()
        if multi:
            keys.extend([k.strip() for k in multi.split(",") if k.strip()])
        wfile = ROOT / "wallets.txt"
        if wfile.exists():
            for line in wfile.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    keys.append(line)
        # dedupe preserve order
        seen: set[str] = set()
        uniq: list[str] = []
        for k in keys:
            if not k.startswith("0x"):
                k = "0x" + k
            kl = k.lower()
            if kl not in seen:
                seen.add(kl)
                uniq.append(k)

        # Parse RPC URLs (comma-separated)
        rpc_single = (os.getenv("RPC_URL") or "https://mainnet.base.org").strip()
        rpc_multi = (os.getenv("RPC_URLS") or "").strip()
        rpc_urls = [rpc_single]
        if rpc_multi:
            rpc_urls.extend([u.strip() for u in rpc_multi.split(",") if u.strip()])

        return cls(
            private_keys=uniq,
            collection_slug=(os.getenv("COLLECTION_SLUG") or "").strip(),
            nft_contract=(os.getenv("NFT_CONTRACT") or "").strip().lower(),
            chain=(os.getenv("CHAIN") or "base").strip(),
            quantity=str(os.getenv("QUANTITY") or "1"),
            token_id=str(os.getenv("TOKEN_ID") or "0"),
            rpc_url=rpc_single,
            rpc_urls=rpc_urls,
            early_fire_ms=int(os.getenv("EARLY_FIRE_MS") or "0"),
            max_fee_gwei=float(os.getenv("MAX_FEE_GWEI") or "1.0"),
            max_priority_gwei=float(os.getenv("MAX_PRIORITY_GWEI") or "0.05"),
            gas_limit=int(os.getenv("GAS_LIMIT") or "300000"),
            warmup_s=float(os.getenv("WARMUP_SECONDS") or "5"),
            hammer_early_s=float(os.getenv("HAMMER_EARLY_SECONDS") or "1.5"),
            schedule_poll_s=float(os.getenv("SCHEDULE_POLL_SECONDS") or "30"),
            force_mint_at=(os.getenv("FORCE_MINT_AT") or "").strip() or None,
            live=_env_bool("LIVE", False),
            dry_fetch=_env_bool("DRY_FETCH", True),
            auth_base=(os.getenv("AUTH_BASE") or "https://opensea.io/__api").rstrip("/"),
            gql_url=(os.getenv("GQL_URL") or "https://gql.opensea.io/graphql").strip(),
            siwe_domain=(os.getenv("SIWE_DOMAIN") or "opensea.io").strip(),
            siwe_uri=(os.getenv("SIWE_URI") or "https://opensea.io/").strip(),
            siwe_chain_id=int(os.getenv("SIWE_CHAIN_ID") or "8453"),
            connector_id=(os.getenv("CONNECTOR_ID") or "injected").strip(),
        )


class OpenSeaClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = requests.Session()
        adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20, max_retries=1)
        self.session.mount('https://', adapter)
        self.session.headers.update(
            {
                "User-Agent": UA,
                "Accept": "application/json",
                "Origin": "https://opensea.io",
                "Referer": "https://opensea.io/",
                "Connection": "keep-alive",
            }
        )

    def auth_post(self, path: str, json_body: dict | None = None, cookies: dict | None = None) -> requests.Response:
        url = f"{self.cfg.auth_base}{path}"
        return self.session.post(
            url,
            json=json_body,
            cookies=cookies,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )

    def get_nonce(self) -> str:
        r = self.auth_post("/auth/siwe/nonce")
        r.raise_for_status()
        return r.json()["nonce"]

    def build_siwe_message(self, address: str, nonce: str, chain_id: int) -> str:
        # OpenSea createMessage: domain host, uri=encodeURI(origin+pathname),
        # statement defaults to TOS/privacy text. Address as provided (use lowercase).
        address = address.lower()
        issued = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        uri = quote(self.cfg.siwe_uri, safe=":/?&=#%")
        # Header matches parseSiwxMessage: optional "Ethereum " account type.
        lines = [
            f"{self.cfg.siwe_domain} wants you to sign in with your Ethereum account:",
            address,
            "",
            "Click to sign in and accept the OpenSea Terms of Service (https://opensea.io/tos) and Privacy Policy (https://opensea.io/privacy).",
            "",
            f"URI: {uri}",
            "Version: 1",
            f"Chain ID: {chain_id}",
            f"Nonce: {nonce}",
            f"Issued At: {issued}",
        ]
        return "\n".join(lines)

    @staticmethod
    def parse_siwe(message: str) -> dict:
        # Mirror parseSiwxMessage from chunk 0g9p8xiss6~4z.js
        lines = message.split("\n")
        import re

        head = re.match(
            r"^(?P<domain>[^ ]+) wants you to sign in with your (?:(?P<accountType>Ethereum|Solana|Bitcoin) )?account:$",
            lines[0] if lines else "",
        )
        addr = re.match(
            r"^(?P<address>(?:0x[0-9a-fA-F]{40}|[1-9A-HJ-NP-Za-km-z]{32,44}))$",
            lines[1] if len(lines) > 1 else "",
        )
        fields = {"uri": "", "version": "", "chainId": "", "nonce": "", "issuedAt": ""}
        prefixes = {
            "uri": "URI: ",
            "version": "Version: ",
            "chainId": "Chain ID: ",
            "nonce": "Nonce: ",
            "issuedAt": "Issued At: ",
        }
        cut = len(lines)
        for i in range(2, len(lines)):
            t = lines[i]
            for k, pref in prefixes.items():
                if not fields[k] and t.startswith(pref):
                    fields[k] = t[len(pref) :]
                    cut = min(cut, i)
        stmt_lines = lines[2:cut]
        while stmt_lines and stmt_lines[0] == "":
            stmt_lines.pop(0)
        while stmt_lines and stmt_lines[-1] == "":
            stmt_lines.pop()
        out = {
            "domain": (head.group("domain") if head else ""),
            "address": (addr.group("address") if addr else ""),
            "statement": "\n".join(stmt_lines),
            "uri": fields["uri"],
            "version": fields["version"],
            "chainId": fields["chainId"],
            "nonce": fields["nonce"],
            "issuedAt": fields["issuedAt"],
        }
        if head and head.group("accountType"):
            out["accountType"] = head.group("accountType")
        return out

    def verify_siwe(self, message: str, signature: str) -> tuple[dict, dict]:
        parsed = self.parse_siwe(message)
        body = {
            "message": parsed,
            "signature": signature,
            "chainArch": "EVM",
            "connectorId": self.cfg.connector_id,
        }
        r = self.auth_post("/auth/siwe/verify", body)
        if not r.ok:
            raise RuntimeError(f"SIWE verify {r.status_code}: {r.text[:500]}")
        cookies = r.cookies.get_dict()
        # also capture set-cookie names from jar
        return r.json(), cookies

    def login_wallet(self, w: Wallet) -> Wallet:
        nonce = self.get_nonce()
        msg = self.build_siwe_message(w.address, nonce, self.cfg.siwe_chain_id)
        signed = w.account.sign_message(encode_defunct(text=msg))
        sig = signed.signature.hex()
        if not sig.startswith("0x"):
            sig = "0x" + sig
        data, cookies = self.verify_siwe(msg, sig)
        w.cookies = cookies
        # token may be cookie-only
        w.access_token = cookies.get("access_token") or (data.get("access_token") if isinstance(data, dict) else None)
        print(f"[auth] {w.address[:10]}… ok cookies={list(cookies.keys())}")
        return w

    def gql(self, query: str, variables: dict | None = None, cookies: dict | None = None, op_name: str | None = None) -> dict:
        headers = {
            "Content-Type": "application/json",
            "x-app-id": "os2-web",
            "Origin": "https://opensea.io",
            "Referer": "https://opensea.io/",
            "User-Agent": UA,
        }
        if cookies and cookies.get("access_token"):
            headers["Cookie"] = f"access_token={cookies['access_token']}"
            # refresh if present
            if cookies.get("refresh_token"):
                headers["Cookie"] += f"; refresh_token={cookies['refresh_token']}"
        payload: dict[str, Any] = {"query": query, "variables": variables or {}}
        if op_name:
            payload["operationName"] = op_name
        r = self.session.post(self.cfg.gql_url, headers=headers, json=payload, timeout=30)
        try:
            data = r.json()
        except Exception:
            raise RuntimeError(f"GQL non-json {r.status_code}: {r.text[:400]}")
        if r.status_code >= 400:
            raise RuntimeError(f"GQL HTTP {r.status_code}: {data}")
        return data

    def drop_schedule(self, slug: str, cookies: dict | None = None) -> dict:
        return self.gql(DROP_SCHEDULE_QUERY, {"slug": slug}, cookies=cookies, op_name="DropSchedule")

    def drop_eligibility(self, slug: str, address: str, cookies: dict | None = None) -> dict:
        return self.gql(
            DROP_ELIGIBILITY_QUERY,
            {"collectionSlug": slug, "address": address},
            cookies=cookies,
            op_name="DropEligibilityQuery",
        )

    def build_swap_batch_query(self, wallets: list[Wallet]) -> str:
        # GraphQL field alias batch — one RTT for N wallets (article technique).
        parts = ["query B {"]
        for i, w in enumerate(wallets):
            # Inline args: schema of swap() is undocumented; match article shape.
            parts.append(
                f'''
  w{i}: swap(
    address: "{w.address.lower()}"
    fromAssets: [{{ asset: {{ chain: "{self.cfg.chain}", contractAddress: "{ZERO}" }} }}]
    toAssets: [{{ asset: {{ chain: "{self.cfg.chain}", contractAddress: "{self.cfg.nft_contract}", tokenId: "{self.cfg.token_id}" }}, quantity: "{self.cfg.quantity}" }}]
    action: MINT
    capabilities: {{ eip7702: false }}
  ) {SWAP_SELECTION}
'''
            )
        parts.append("}")
        return "\n".join(parts)

    def fetch_mint_calldata(self, wallets: list[Wallet], cookies: dict | None = None) -> dict[str, Any]:
        q = self.build_swap_batch_query(wallets)
        # use first wallet cookies if not provided
        c = cookies or (wallets[0].cookies if wallets else None)
        return self.gql(q, cookies=c, op_name="B")


def load_wallets(keys: list[str]) -> list[Wallet]:
    out: list[Wallet] = []
    for k in keys:
        acc = Account.from_key(k)
        out.append(Wallet(key=k, address=acc.address.lower(), account=acc))
    return out


def warm_nonces(w3: Web3, wallets: list[Wallet]) -> None:
    def one(w: Wallet):
        try:
            w.nonce = w3.eth.get_transaction_count(Web3.to_checksum_address(w.address))
            return w.address, w.nonce, None
        except Exception as e:
            return w.address, None, str(e)

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, max(1, len(wallets)))) as ex:
        for addr, nonce, err in ex.map(one, wallets):
            if err:
                print(f"[warmup] nonce fail {addr[:10]}… {err}")
            else:
                print(f"[warmup] nonce {addr[:10]}… = {nonce}")


# NEW: Warm TLS connections to all RPCs (persistent, keep-alive)
_RPC_SESSION: requests.Session | None = None

def _rpc_session() -> requests.Session:
    """A single keep-alive session reused for every RPC blast (persistent sockets)."""
    global _RPC_SESSION
    if _RPC_SESSION is None:
        _RPC_SESSION = requests.Session()
        adapter = HTTPAdapter(pool_connections=32, pool_maxsize=32, max_retries=0)
        _RPC_SESSION.mount('https://', adapter)
        _RPC_SESSION.mount('http://', adapter)
    return _RPC_SESSION

def warm_connections(rpc_urls: list[str]) -> None:
    """Pre-establish TCP/TLS to every RPC and keep the sockets open (keep-alive).
    The fire moment then reuses an already-warm socket — zero handshake cost."""
    print(f"[warm] warming {len(rpc_urls)} connections...")
    session = _rpc_session()
    body = json.dumps({
        "jsonrpc": "2.0",
        "method": "eth_sendRawTransaction",
        "params": ["0x00"],
        "id": 1,
    }).encode()

    def warm_one(url):
        try:
            session.post(url, data=body, headers={"content-type": "application/json"}, timeout=5)
        except Exception:
            pass  # ignore errors, handshake is the point

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(rpc_urls)) as ex:
        list(ex.map(warm_one, rpc_urls))

    print(f"[warm] connections hot (keep-alive)")


# RPC resolver: probe every endpoint, keep matching + send-only, drop wrong chain
def plan_rpcs(rpc_urls: list[str], expected_chain_id: int) -> tuple[list[str], list[dict]]:
    """Probe all RPCs, return (usable_urls, dropped).
    usable = endpoints matching expected chain + send-only (no chain reply but reachable).
    dropped = endpoints that report a different chain.
    """
    def probe(url):
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps({"jsonrpc": "2.0", "method": "eth_chainId", "params": [], "id": 1}).encode(),
                headers={"content-type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = json.loads(resp.read().decode())
                cid = data.get("result")
                if cid:
                    return {"url": url, "chain_id": int(cid, 16)}
                return {"url": url, "chain_id": None}
        except Exception as e:
            return {"url": url, "chain_id": None, "error": str(e)[:120]}

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(rpc_urls)) as ex:
        results = list(ex.map(probe, rpc_urls))

    usable, dropped = [], []
    for r in results:
        if r.get("chain_id") == expected_chain_id:
            usable.append(r["url"])
        elif r.get("chain_id") is None:
            usable.append(r["url"])  # send-only candidate: no reply, keep for blast
        else:
            dropped.append({"url": r["url"], "chain_id": r["chain_id"]})
    return usable, dropped


# NEW: Blast raw tx to all RPCs (fire-and-forget, persistent keep-alive sockets)
def blast_to_all(raw_tx: str, rpc_urls: list[str]) -> str:
    """Send raw tx to all RPCs simultaneously. Returns tx hash immediately.
    Fire-and-forget: detached threads + pre-warmed keep-alive session, so the
    caller returns in sub-ms without paying for TLS handshake or RPC round-trip."""
    from web3 import Web3
    tx_hash = Web3.keccak(hexstr=raw_tx).hex()
    if not tx_hash.startswith("0x"):
        tx_hash = "0x" + tx_hash

    # Pre-compute body once (mirrors nft-public-mint's prepareBlast)
    body = json.dumps({
        "jsonrpc": "2.0",
        "method": "eth_sendRawTransaction",
        "params": [raw_tx],
        "id": 1,
    }).encode()
    session = _rpc_session()

    def send_one(url):
        try:
            session.post(url, data=body, headers={"content-type": "application/json"}, timeout=10)
        except Exception:
            pass  # fire-and-forget

    # Detached threads: return immediately, let OS finish the send in background.
    for url in rpc_urls:
        threading.Thread(target=send_one, args=(url,), daemon=True).start()

    return tx_hash


# Poll for tx receipt on the primary RPC (verify success/failure)
def wait_for_receipt(w3: Web3, tx_hash: str, timeout_s: float = 30.0) -> dict | None:
    """Poll eth_getTransactionReceipt until mined or timeout. Returns receipt dict or None."""
    start = time.time()
    while time.time() - start < timeout_s:
        try:
            rcpt = w3.eth.get_transaction_receipt(tx_hash)
            if rcpt:
                return {
                    "block": rcpt["blockNumber"],
                    "status": "SUCCESS" if rcpt["status"] == 1 else "REVERTED",
                    "gasUsed": rcpt["gasUsed"],
                }
        except Exception:
            pass
        time.sleep(0.5)
    return None


# NEW: Precise wait with spin-loop for final milliseconds
def precise_wait(target_time: float, early_ms: int = 0) -> None:
    """Wait until target_time - early_ms, with spin-wait for final 100ms."""
    fire_time = target_time - (early_ms / 1000.0)
    
    # Coarse wait until 100ms before
    while True:
        remaining = fire_time - time.time()
        if remaining <= 0.1:
            break
        time.sleep(0.05)
    
    # Spin-wait for final precision
    while time.time() < fire_time:
        pass


def keepalive(cfg: Config, cookies: dict | None) -> None:
    try:
        requests.get(
            cfg.gql_url,
            headers={"x-app-id": "os2-web", "User-Agent": UA, "Origin": "https://opensea.io"},
            timeout=5,
        )
    except Exception:
        pass
    try:
        requests.post(
            cfg.rpc_url,
            json={"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []},
            timeout=5,
        )
    except Exception:
        pass


def _parse_ts(st) -> float | None:
    if not st:
        return None
    try:
        if isinstance(st, str):
            return _iso_to_ts(st)
        v = float(st)
        return v / 1000.0 if v > 1e12 else v
    except Exception:
        return None


def pick_mint_ts(cfg: Config, schedule: dict) -> float | None:
    """Return fire time. None = fire now (open stage / no schedule)."""
    if cfg.force_mint_at:
        return _iso_to_ts(cfg.force_mint_at)
    drop = ((schedule.get("data") or {}).get("dropBySlug")) or None
    if not drop:
        return None
    stages = drop.get("stages") or []
    now = _now()
    open_starts: list[float] = []
    future_starts: list[float] = []
    for s in stages:
        ts = _parse_ts(s.get("startTime"))
        te = _parse_ts(s.get("endTime"))
        if ts is None:
            continue
        if te is not None and ts <= now < te:
            open_starts.append(ts)
        elif ts > now - 2:
            future_starts.append(ts)
    # already live → fire immediately (no wait on past start)
    if open_starts:
        return None
    if future_starts:
        return min(future_starts)
    # all stages ended → None (caller still may probe swap once)
    return None


def extract_txs(gql_resp: dict, wallets: list[Wallet]) -> list[tuple[Wallet, dict]]:
    data = gql_resp.get("data") or {}
    errors = gql_resp.get("errors")
    if errors and not data:
        blob = json.dumps(errors)
        if "Too Many Requests" in blob or "rate" in blob.lower():
            return []
        raise RuntimeError(f"swap GQL errors: {errors}")
    out: list[tuple[Wallet, dict]] = []
    for i, w in enumerate(wallets):
        node = data.get(f"w{i}")
        if node is None:
            print(f"[swap] w{i} {w.address[:10]}… missing node errors={errors}")
            continue
        # node may be error union
        if isinstance(node, dict) and node.get("errors"):
            print(f"[swap] w{i} node errors: {node.get('errors')}")
        actions = (node or {}).get("actions") or []
        tsd = None
        for a in actions:
            if a and a.get("transactionSubmissionData"):
                tsd = a["transactionSubmissionData"]
                break
        if not tsd:
            print(f"[swap] w{i} {w.address[:10]}… no transactionSubmissionData: {json.dumps(node)[:300]}")
            continue
        out.append((w, tsd))
    return out


def sign_only(
    w3: Web3,
    cfg: Config,
    w: Wallet,
    tsd: dict,
    chain_id: int,
) -> tuple[str, str]:
    """Sign transaction without sending. Returns (raw_tx_hex, tx_hash)."""
    to = tsd.get("to")
    data = tsd.get("data")
    value = int(tsd.get("value") or "0")
    if not to or not data:
        raise RuntimeError(f"bad tsd: {tsd}")
    if w.nonce is None:
        w.nonce = w3.eth.get_transaction_count(Web3.to_checksum_address(w.address))

    tx = {
        "chainId": chain_id,
        "nonce": w.nonce,
        "to": Web3.to_checksum_address(to),
        "data": data,
        "value": value,
        "gas": cfg.gas_limit,
        "gasPrice": w3.to_wei(cfg.max_fee_gwei, "gwei"),
    }
    signed = w.account.sign_transaction(tx)
    raw = signed.raw_transaction.hex()
    if not raw.startswith("0x"):
        raw = "0x" + raw
    tx_hash = signed.hash.hex()
    if not tx_hash.startswith("0x"):
        tx_hash = "0x" + tx_hash
    return raw, tx_hash


def sign_and_maybe_send(
    w3: Web3,
    cfg: Config,
    w: Wallet,
    tsd: dict,
    chain_id: int,
) -> str | None:
    to = tsd.get("to")
    data = tsd.get("data")
    value = int(tsd.get("value") or "0")
    if not to or not data:
        raise RuntimeError(f"bad tsd: {tsd}")
    if w.nonce is None:
        w.nonce = w3.eth.get_transaction_count(Web3.to_checksum_address(w.address))

    tx = {
        "chainId": chain_id,
        "nonce": w.nonce,
        "to": Web3.to_checksum_address(to),
        "data": data,
        "value": value,
        "gas": cfg.gas_limit,
        "gasPrice": w3.to_wei(cfg.max_fee_gwei, "gwei"),
    }
    signed = w.account.sign_transaction(tx)
    raw = signed.raw_transaction.hex()
    if not raw.startswith("0x"):
        raw = "0x" + raw
    tx_hash = signed.hash.hex()
    if not tx_hash.startswith("0x"):
        tx_hash = "0x" + tx_hash

    print(
        f"[tx] {w.address[:10]}… to={to[:10]}… value={value} nonce={w.nonce} "
        f"hash={tx_hash} LIVE={int(cfg.live)}"
    )
    if not cfg.live:
        print(f"[dry] raw_tx_len={len(raw)} (not broadcast)")
        return None
    
    # NEW: Use blast_to_all for multi-RPC
    if len(cfg.rpc_urls) > 1:
        tx_hash = blast_to_all(raw, cfg.rpc_urls)
        print(f"[blast] sent to {len(cfg.rpc_urls)} RPCs")
    else:
        sent = w3.eth.send_raw_transaction(signed.raw_transaction)
        tx_hash = sent.hex()
        print(f"[sent] {tx_hash}")
    
    w.nonce += 1
    return tx_hash


def wait_until(ts: float, label: str) -> None:
    while True:
        left = ts - _now()
        if left <= 0:
            return
        sleep = min(left, 1.0 if left < 10 else 5.0)
        print(f"[wait] {label} in {left:.2f}s")
        time.sleep(sleep)


# Permanent swap failures — don't spin hammer
_FATAL_SWAP = (
    "InsufficientFundError",
    "InsufficientMintsRemainingError",
    "DropNotFoundError",
    "MinterNotEligible",
    "CollectionNotFoundError",
    "DropNotMintingError",  # past/ended; pre-start hammer still sees this briefly but we only fire when open or forced
)


import concurrent.futures

def hammer_swap(client: OpenSeaClient, wallets: list[Wallet], deadline: float | None = None) -> dict:
    """Retry swap until calldata or timeout. Transient DropNotMinting expected pre-start."""
    last_err = None
    last_resp: dict = {}
    
    def _fire():
        return client.fetch_mint_calldata(wallets)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        while True:
            futures = [executor.submit(_fire) for _ in range(4)]
            for fut in concurrent.futures.as_completed(futures):
                try:
                    resp = fut.result()
                    last_resp = resp
                    pairs = extract_txs(resp, wallets)
                    if pairs:
                        print(f"[hammer] got {len(pairs)}/{len(wallets)} calldata")
                        return resp
                    blob = json.dumps(resp)
                    if any(f in blob for f in _FATAL_SWAP):
                        print(f"[hammer] fatal swap response: {blob[:300]}")
                        return resp
                    if "DropNotMinting" in blob or "not minting" in blob.lower():
                        last_err = "DropNotMinting"
                    elif "Too Many Requests" in blob:
                        last_err = "rate_limit"
                        time.sleep(1.0)
                    else:
                        last_err = blob[:400]
                except Exception as e:
                    last_err = str(e)
                    print(f"[hammer] err: {e}")

            if deadline and _now() >= deadline:
                print(f"[hammer] timeout. Last err: {last_err}")
                break
            time.sleep(0.05)
            
    return last_resp


# NEW: Public mint bypass functions
def fetch_public_drop(w3: Web3, nft_contract: str, chain_id: int) -> dict | None:
    """Fetch public drop info from SeaDrop contract (no OpenSea needed)."""
    seadrop_addr = SEADROP_ADDRESSES.get(chain_id)
    if not seadrop_addr:
        print(f"[public] no SeaDrop address for chain {chain_id}")
        return None
    
    seadrop = w3.eth.contract(
        address=Web3.to_checksum_address(seadrop_addr),
        abi=[
            {
                "inputs": [{"internalType": "address", "name": "nftContract", "type": "address"}],
                "name": "getPublicDrop",
                "outputs": [
                    {
                        "internalType": "tuple",
                        "name": "",
                        "type": "tuple",
                        "components": [
                            {"internalType": "uint80", "name": "mintPrice", "type": "uint80"},
                            {"internalType": "uint48", "name": "startTime", "type": "uint48"},
                            {"internalType": "uint48", "name": "endTime", "type": "uint48"},
                            {"internalType": "uint16", "name": "maxTotalMintableByWallet", "type": "uint16"},
                            {"internalType": "uint16", "name": "feeBps", "type": "uint16"},
                            {"internalType": "bool", "name": "restrictFeeRecipients", "type": "bool"},
                        ],
                    }
                ],
                "stateMutability": "view",
                "type": "function",
            },
            {
                "inputs": [{"internalType": "address", "name": "nftContract", "type": "address"}],
                "name": "getAllowedFeeRecipients",
                "outputs": [{"internalType": "address[]", "name": "", "type": "address[]"}],
                "stateMutability": "view",
                "type": "function",
            },
        ],
    )
    
    try:
        raw = seadrop.functions.getPublicDrop(Web3.to_checksum_address(nft_contract)).call()
        drop = {
            "mintPrice": raw[0],
            "startTime": raw[1],
            "endTime": raw[2],
            "maxTotalMintableByWallet": raw[3],
            "feeBps": raw[4],
            "restrictFeeRecipients": raw[5],
        }
        # Check if drop is actually configured
        if drop["startTime"] == 0 and drop["endTime"] == 0 and drop["maxTotalMintableByWallet"] == 0:
            return None
        
        # Resolve fee recipient
        try:
            allowed = seadrop.functions.getAllowedFeeRecipients(Web3.to_checksum_address(nft_contract)).call()
            if allowed:
                drop["feeRecipient"] = allowed[0]
            else:
                drop["feeRecipient"] = OPENSEA_FEE_RECIPIENT if not drop["restrictFeeRecipients"] else None
        except Exception:
            drop["feeRecipient"] = OPENSEA_FEE_RECIPIENT if not drop["restrictFeeRecipients"] else None
        
        return drop
    except Exception as e:
        print(f"[public] fetch_public_drop error: {e}")
        return None


def build_public_mint(w3: Web3, cfg: Config, w: Wallet, chain_id: int) -> tuple[dict, str, str] | None:
    """Build signed tx for public mint (no OpenSea signature needed).
    Returns (tx_dict, raw_tx_hex, tx_hash) or None if not possible."""
    drop = fetch_public_drop(w3, cfg.nft_contract, chain_id)
    if not drop:
        return None
    if not drop.get("feeRecipient"):
        print(f"[public] no fee recipient available")
        return None
    
    seadrop_addr = SEADROP_ADDRESSES.get(chain_id)
    if not seadrop_addr:
        return None
    
    # Encode mintPublic(nftContract, feeRecipient, minterIfNotPayer, quantity)
    seadrop = w3.eth.contract(
        address=Web3.to_checksum_address(seadrop_addr),
        abi=[
            {
                "inputs": [
                    {"internalType": "address", "name": "nftContract", "type": "address"},
                    {"internalType": "address", "name": "feeRecipient", "type": "address"},
                    {"internalType": "address", "name": "minterIfNotPayer", "type": "address"},
                    {"internalType": "uint256", "name": "quantity", "type": "uint256"},
                ],
                "name": "mintPublic",
                "outputs": [],
                "stateMutability": "payable",
                "type": "function",
            }
        ],
    )
    
    quantity = int(cfg.quantity)
    value = drop["mintPrice"] * quantity
    
    if w.nonce is None:
        w.nonce = w3.eth.get_transaction_count(Web3.to_checksum_address(w.address))
    
    tx = seadrop.functions.mintPublic(
        Web3.to_checksum_address(cfg.nft_contract),
        Web3.to_checksum_address(drop["feeRecipient"]),
        Web3.to_checksum_address(ZERO),  # minterIfNotPayer = address(0)
        quantity
    ).build_transaction({
        "chainId": chain_id,
        "nonce": w.nonce,
        "value": value,
        "gas": cfg.gas_limit,
        "gasPrice": w3.to_wei(cfg.max_fee_gwei, "gwei"),
    })
    
    signed = w.account.sign_transaction(tx)
    raw = signed.raw_transaction.hex()
    if not raw.startswith("0x"):
        raw = "0x" + raw
    tx_hash = signed.hash.hex()
    if not tx_hash.startswith("0x"):
        tx_hash = "0x" + tx_hash
    
    return tx, raw, tx_hash


def run_pipeline(cfg: Config, stage: str) -> int:
    if not cfg.private_keys:
        print("ERROR: no PRIVATE_KEY / wallets.txt")
        return 2
    wallets = load_wallets(cfg.private_keys)
    print(f"[boot] wallets={len(wallets)} LIVE={int(cfg.live)} chain={cfg.chain} slug={cfg.collection_slug or '-'}")
    print(f"[boot] rpc_urls={len(cfg.rpc_urls)} early_fire_ms={cfg.early_fire_ms}")

    client = OpenSeaClient(cfg)
    w3 = Web3(Web3.HTTPProvider(cfg.rpc_url, request_kwargs={"timeout": 20}))
    if not w3.is_connected():
        print(f"WARN: RPC not connected: {cfg.rpc_url}")

    if cfg.live:
        ok, detail = rpc_health(cfg.rpc_url, chain_expect=cfg.siwe_chain_id, want_tx=True)
        print(f"[rpc-health] {'OK' if ok else 'FAIL'} {detail}")
        if not ok:
            print(f"[rpc-health] FATAL: RPC unusable for LIVE on {cfg.rpc_url} — aborting")
            return 2

    # Plan RPCs: probe all endpoints, drop wrong chain, keep send-only
    if len(cfg.rpc_urls) > 1:
        usable, dropped = plan_rpcs(cfg.rpc_urls, cfg.siwe_chain_id)
        if dropped:
            for d in dropped:
                print(f"[rpc-plan] DROPPED {d['url']} (chain {d['chain_id']}, expected {cfg.siwe_chain_id})")
        cfg.rpc_urls = usable
        print(f"[rpc-plan] {len(cfg.rpc_urls)} usable RPCs: {cfg.rpc_urls}")

    # Get schedule first to determine which phase is active
    # Priority: allowlist/FCFS > public (allowlist always comes first in real drops)
    chain_id = cfg.siwe_chain_id
    if w3.is_connected():
        chain_id = w3.eth.chain_id
    
    # Auth first (needed for schedule check)
    primary = client.login_wallet(wallets[0])
    for w in wallets[1:]:
        try:
            client.login_wallet(w)
        except Exception as e:
            print(f"[auth] skip {w.address[:10]}… {e}")

    if stage == "auth":
        print("[done] auth only")
        return 0

    # Get schedule to determine active phase
    if cfg.collection_slug:
        sched = client.drop_schedule(cfg.collection_slug, primary.cookies)
        print("[schedule]", json.dumps(sched)[:800])
        try:
            elig = client.drop_eligibility(cfg.collection_slug, primary.address, primary.cookies)
            print("[elig]", json.dumps(elig)[:800])
        except Exception as e:
            print(f"[elig] skip: {e}")
    else:
        sched = {"data": {"dropBySlug": None}}
        print("[schedule] no COLLECTION_SLUG — use FORCE_MINT_AT or fire now")

    if stage == "schedule":
        return 0

    if not cfg.nft_contract:
        print("ERROR: NFT_CONTRACT required for swap/mint stages")
        return 2

    # Determine which phase is ACTIVE (allowlist takes priority over public)
    drop = ((sched.get("data") or {}).get("dropBySlug")) or None
    stages = (drop.get("stages") or []) if drop else []
    now = _now()
    
    allowlist_active = False
    public_active = False
    
    for s in stages:
        stage_type = (s.get("stageType") or "").lower()
        start = _parse_ts(s.get("startTime"))
        end = _parse_ts(s.get("endTime"))
        
        if start and end and start <= now < end:
            if "allowlist" in stage_type or "fcfs" in stage_type or "early" in stage_type:
                allowlist_active = True
                print(f"[phase] ALLOWLIST ACTIVE ({datetime.fromtimestamp(start, tz=timezone.utc).isoformat()} - {datetime.fromtimestamp(end, tz=timezone.utc).isoformat()})")
            elif "public" in stage_type:
                public_active = True
                print(f"[phase] PUBLIC ACTIVE ({datetime.fromtimestamp(start, tz=timezone.utc).isoformat()} - {datetime.fromtimestamp(end, tz=timezone.utc).isoformat()})")
    
    # If no active phase detected, check for upcoming phases
    if not allowlist_active and not public_active:
        upcoming_allowlist = None
        upcoming_public = None
        for s in stages:
            stage_type = (s.get("stageType") or "").lower()
            start = _parse_ts(s.get("startTime"))
            if start and start > now:
                if "allowlist" in stage_type or "fcfs" in stage_type or "early" in stage_type:
                    upcoming_start = _parse_ts(upcoming_allowlist.get("startTime")) if upcoming_allowlist else None
                    if not upcoming_allowlist or (upcoming_start is not None and start < upcoming_start):
                        upcoming_allowlist = s
                elif "public" in stage_type:
                    upcoming_start = _parse_ts(upcoming_public.get("startTime")) if upcoming_public else None
                    if not upcoming_public or (upcoming_start is not None and start < upcoming_start):
                        upcoming_public = s
        
        # Prefer allowlist if both exist (allowlist always comes first)
        if upcoming_allowlist:
            allowlist_start = _parse_ts(upcoming_allowlist.get('startTime'))
            if allowlist_start:
                print(f"[phase] ALLOWLIST UPCOMING at {datetime.fromtimestamp(allowlist_start, tz=timezone.utc).isoformat()}")
        elif upcoming_public:
            public_start = _parse_ts(upcoming_public.get('startTime'))
            if public_start:
                print(f"[phase] PUBLIC UPCOMING at {datetime.fromtimestamp(public_start, tz=timezone.utc).isoformat()}")
    
    # Route to appropriate path based on active phase
    # Priority: allowlist > public > fallback to allowlist path (OpenSea)
    use_public_bypass = False
    public_drop = None  # Initialize to avoid unbound variable
    
    if allowlist_active:
        print("[route] using ALLOWLIST path (OpenSea GraphQL with signature)")
        use_public_bypass = False
    elif public_active:
        # Check if public drop exists on-chain
        public_drop = fetch_public_drop(w3, cfg.nft_contract, chain_id) if cfg.nft_contract else None
        if public_drop:
            print(f"[route] using PUBLIC bypass path (SeaDrop contract, no OpenSea)")
            print(f"[public] price={public_drop['mintPrice']} wei, max={public_drop['maxTotalMintableByWallet']}/wallet")
            use_public_bypass = True
        else:
            print("[route] public phase active but no on-chain public drop found, fallback to allowlist path")
            use_public_bypass = False
    else:
        # No active phase, try public bypass as last resort
        public_drop = fetch_public_drop(w3, cfg.nft_contract, chain_id) if cfg.nft_contract else None
        if public_drop:
            print(f"[route] no active phase but public drop exists, using PUBLIC bypass")
            use_public_bypass = True
        else:
            print("[route] no active phase, using ALLOWLIST path (OpenSea)")
            use_public_bypass = False
    
    if use_public_bypass and public_drop:
        print(f"[public] SeaDrop public stage detected! Bypassing OpenSea GraphQL")
        print(f"[public] price={public_drop['mintPrice']} wei, max={public_drop['maxTotalMintableByWallet']}/wallet")
        print(f"[public] start={datetime.fromtimestamp(public_drop['startTime'], tz=timezone.utc).isoformat()}")
        print(f"[public] end={datetime.fromtimestamp(public_drop['endTime'], tz=timezone.utc).isoformat()}")
        
        # Warm connections
        warm_connections(cfg.rpc_urls)
        
        # Warm nonces
        warm_nonces(w3, wallets)
        
        # Pre-sign all txs
        print(f"[public] pre-signing {len(wallets)} transactions...")
        signed_txs = []
        for w in wallets:
            result = build_public_mint(w3, cfg, w, chain_id)
            if result:
                tx, raw, tx_hash = result
                signed_txs.append((w, raw, tx_hash))
                print(f"[public] {w.address[:10]}… signed hash={tx_hash[:20]}…")
            else:
                print(f"[public] {w.address[:10]}… failed to build tx")
        
        if not signed_txs:
            print("[public] no signed txs, aborting")
            return 1
        
        # Wait for mint time
        now = _now()
        if public_drop["startTime"] > now:
            wait_time = public_drop["startTime"]
            print(f"[public] waiting until {datetime.fromtimestamp(wait_time, tz=timezone.utc).isoformat()}")
            precise_wait(wait_time, cfg.early_fire_ms)
        
        # Blast to all RPCs
        all_ok = True
        if cfg.live:
            print(f"[public] FIRING to {len(cfg.rpc_urls)} RPCs!")
            for w, raw, tx_hash in signed_txs:
                if len(cfg.rpc_urls) > 1:
                    blast_hash = blast_to_all(raw, cfg.rpc_urls)
                    print(f"[blast] {w.address[:10]}… sent to {len(cfg.rpc_urls)} RPCs hash={blast_hash[:20]}…")
                    print(f"[tx] TX: {tx_hash}")
                    print(f"[tx] LINK: {explorer_link(cfg.chain, tx_hash)}")
                else:
                    try:
                        sent = w3.eth.send_raw_transaction(bytes.fromhex(raw[2:]))
                        tx_hash = sent.hex()
                        print(f"[sent] {w.address[:10]}… {tx_hash}")
                        print(f"[tx] TX: {tx_hash}")
                        print(f"[tx] LINK: {explorer_link(cfg.chain, tx_hash)}")
                    except Exception as e:
                        print(f"[sent] FAIL {w.address[:10]}… {e}")
                        all_ok = False
                w.nonce += 1
            # Verify receipts
            for w, raw, tx_hash in signed_txs:
                rcpt = wait_for_receipt(w3, tx_hash, timeout_s=20.0)
                if rcpt:
                    ok = rcpt["status"] == "SUCCESS"
                    if not ok:
                        all_ok = False
                    print(f"[receipt] {w.address[:10]}… {rcpt['status']} block={rcpt['block']} gasUsed={rcpt['gasUsed']}")
                else:
                    print(f"[receipt] {w.address[:10]}… not mined in 20s (may still land)")
                    all_ok = False
            if all_ok:
                print("[done] public mint fired OK")
            else:
                print("[done] public mint fired — SOME TX FAILED (see receipt)")
            return 0 if all_ok else 1
        else:
            print("[dry] would fire (LIVE=0)")
        
        return 0
    
    # Fallback to OpenSea allowlist path
    print("[opensea] no public stage, using allowlist path")
    
    # auth primary wallet (cookies shared for GQL; multi-wallet still needs per-address swap)
    primary = client.login_wallet(wallets[0])
    # optional: auth all (ponytail: only primary cookies for GQL; swap address is per-wallet)
    for w in wallets[1:]:
        try:
            client.login_wallet(w)
        except Exception as e:
            print(f"[auth] skip {w.address[:10]}… {e}")

    if stage == "auth":
        print("[done] auth only")
        return 0

    if cfg.collection_slug:
        sched = client.drop_schedule(cfg.collection_slug, primary.cookies)
        print("[schedule]", json.dumps(sched)[:800])
        try:
            elig = client.drop_eligibility(cfg.collection_slug, primary.address, primary.cookies)
            print("[elig]", json.dumps(elig)[:800])
        except Exception as e:
            print(f"[elig] skip: {e}")
    else:
        sched = {"data": {"dropBySlug": None}}
        print("[schedule] no COLLECTION_SLUG — use FORCE_MINT_AT or fire now")

    if stage == "schedule":
        return 0

    if not cfg.nft_contract:
        print("ERROR: NFT_CONTRACT required for swap/mint stages")
        return 2

    mint_ts = pick_mint_ts(cfg, sched)
    if mint_ts:
        print(f"[schedule] mint_ts={datetime.fromtimestamp(mint_ts, tz=timezone.utc).isoformat()} ({mint_ts})")
    else:
        print("[schedule] no mint time — fire immediately after warm-up")

    if stage == "warmup":
        if w3.is_connected():
            warm_nonces(w3, wallets)
            print("[chainId]", w3.eth.chain_id)
        keepalive(cfg, primary.cookies)
        return 0

    # full fire path
    if mint_ts:
        warm_at = mint_ts - cfg.warmup_s
        fetch_at = mint_ts - 2.0  # T-2s: fetch calldata (osnm-z strategy)
        if _now() < warm_at:
            wait_until(warm_at, "warmup")
    else:
        fetch_at = _now()
        print("[schedule] fire now (open stage or no future start)")

    if w3.is_connected():
        warm_nonces(w3, wallets)
        chain_id = w3.eth.chain_id
        if cfg.live and any(w.nonce is None for w in wallets):
            print(f"[warmup] FATAL: nonce fetch failed on {cfg.rpc_url} — aborting LIVE (RPC likely broken)")
            return 2
    else:
        chain_id = cfg.siwe_chain_id
    keepalive(cfg, primary.cookies)
    
    # Warm connections before fire
    warm_connections(cfg.rpc_urls)

    # T-2s: fetch calldata early (osnm-z strategy)
    if mint_ts and _now() < fetch_at:
        wait_until(fetch_at, "fetch calldata")

    if not cfg.dry_fetch and not cfg.live:
        print("[done] DRY_FETCH=0 and LIVE=0 — nothing to do after warm-up")
        return 0

    # Fetch calldata with retries
    if mint_ts and mint_ts > _now():
        deadline = mint_ts + 15
    else:
        deadline = _now() + 8
    resp = hammer_swap(client, wallets, deadline=deadline)
    pairs = extract_txs(resp, wallets)
    if not pairs:
        blob = json.dumps(resp)
        print("[swap] no calldata")
        print(blob[:2000])
        if any(f in blob for f in _FATAL_SWAP) or "DropNotMinting" in blob:
            print("[done] OpenSea responded (no mintable calldata for this wallet/drop)")
            return 0
        if "Too Many Requests" in blob:
            print("[done] rate-limited by OpenSea — retry later")
            return 0
        return 1

    if stage == "fetch":
        for w, tsd in pairs:
            print(f"[fetch] {w.address} -> {json.dumps(tsd)[:300]}")
        return 0

    # Pre-sign all transactions immediately after getting calldata
    print(f"[presign] signing {len(pairs)} transactions...")
    signed_txs = []
    for w, tsd in pairs:
        try:
            raw, tx_hash = sign_only(w3, cfg, w, tsd, chain_id)
            signed_txs.append((w, raw, tx_hash))
            print(f"[presign] {w.address[:10]}… signed hash={tx_hash[:20]}…")
        except Exception as e:
            print(f"[presign] fail {w.address[:10]}… {e}")
    
    if not signed_txs:
        print("[presign] no signed transactions, aborting")
        return 1

    # Wait for exact mint time with spin-wait precision
    if mint_ts and mint_ts > _now():
        print(f"[wait] spin-wait until {datetime.fromtimestamp(mint_ts, tz=timezone.utc).isoformat()}")
        precise_wait(mint_ts, cfg.early_fire_ms)

    # Blast pre-signed transactions to all RPCs
    all_ok = True
    if cfg.live:
        print(f"[fire] blasting {len(signed_txs)} pre-signed transactions to {len(cfg.rpc_urls)} RPCs!")
        for w, raw, tx_hash in signed_txs:
            if len(cfg.rpc_urls) > 1:
                blast_hash = blast_to_all(raw, cfg.rpc_urls)
                print(f"[blast] {w.address[:10]}… sent to {len(cfg.rpc_urls)} RPCs hash={blast_hash[:20]}…")
                print(f"[tx] TX: {tx_hash}")
                print(f"[tx] LINK: {explorer_link(cfg.chain, tx_hash)}")
            else:
                try:
                    sent = w3.eth.send_raw_transaction(bytes.fromhex(raw[2:]))
                    tx_hash = sent.hex()
                    print(f"[sent] {w.address[:10]}… {tx_hash}")
                    print(f"[tx] TX: {tx_hash}")
                    print(f"[tx] LINK: {explorer_link(cfg.chain, tx_hash)}")
                except Exception as e:
                    print(f"[sent] FAIL {w.address[:10]}… {e}")
                    all_ok = False
            w.nonce += 1
        # Verify receipts
        for w, raw, tx_hash in signed_txs:
            rcpt = wait_for_receipt(w3, tx_hash, timeout_s=20.0)
            if rcpt:
                ok = rcpt["status"] == "SUCCESS"
                if not ok:
                    all_ok = False
                print(f"[receipt] {w.address[:10]}… {rcpt['status']} block={rcpt['block']} gasUsed={rcpt['gasUsed']}")
            else:
                print(f"[receipt] {w.address[:10]}… not mined in 20s (may still land)")
                all_ok = False
        if all_ok:
            print("[done] allowlist mint fired OK")
        else:
            print("[done] allowlist mint fired — SOME TX FAILED (see receipt)")
        return 0 if all_ok else 1
    else:
        print("[dry] would fire (LIVE=0)")
    
    return 0


def rpc_health(rpc_url: str, chain_expect: int | None = None, want_tx: bool = False) -> tuple[bool, str]:
    """Check RPC: chainId, balance/count methods, and (optionally) send_raw support.
    Returns (ok, detail). want_tx probes eth_sendRawTransaction with a dummy payload."""
    try:
        w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 10}))
        if not w3.is_connected():
            return False, "not connected"
        cid = w3.eth.chain_id
        detail = f"chainId={cid}"
        w3.eth.block_number
        w3.eth.gas_price
        if chain_expect and cid != chain_expect:
            return False, f"{detail} != expected {chain_expect}"
        if want_tx:
            try:
                w3.eth.send_raw_transaction(b"\x00")
                return False, f"{detail} send_raw accepted dummy (unexpected)"
            except Exception as e:
                msg = str(e)
                if "method" in msg.lower() and ("does not exist" in msg.lower() or "not available" in msg.lower() or "-32601" in msg):
                    return False, f"{detail} send_raw NOT supported: {msg[:80]}"
                detail += " send_raw OK"
        return True, detail
    except Exception as e:
        return False, str(e)[:120]


def self_check() -> None:
    # pure unit checks — no network
    c = OpenSeaClient(
        Config(
            private_keys=[],
            collection_slug="",
            nft_contract="0x" + "1" * 40,
            chain="base",
            quantity="1",
            token_id="0",
            rpc_url="http://127.0.0.1",
            rpc_urls=["http://127.0.0.1"],
            early_fire_ms=0,
            max_fee_gwei=1,
            max_priority_gwei=0.1,
            gas_limit=21000,
            warmup_s=5,
            hammer_early_s=1.5,
            schedule_poll_s=30,
            force_mint_at=None,
            live=False,
            dry_fetch=True,
            auth_base="https://opensea.io/__api",
            gql_url="https://gql.opensea.io/graphql",
            siwe_domain="opensea.io",
            siwe_uri="https://opensea.io/",
            siwe_chain_id=8453,
            connector_id="injected",
        )
    )
    msg = c.build_siwe_message("0xabcDEF0000000000000000000000000000000001", "nonce123", 8453)
    parsed = c.parse_siwe(msg)
    assert parsed["domain"] == "opensea.io", parsed
    assert parsed["address"].lower() == "0xabcdef0000000000000000000000000000000001"
    assert parsed["nonce"] == "nonce123"
    assert parsed["uri"].startswith("https://opensea.io")
    assert "opensea.io/tos" in parsed["statement"]
    w = Wallet(key="0x" + "11" * 32, address="0x" + "22" * 20, account=None)
    q = c.build_swap_batch_query([w])
    assert "w0: swap" in q and "action: MINT" in q and "query B" in q
    print("self_check OK")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="OpenSea FCFS minter (dry by default)")
    p.add_argument(
        "stage",
        nargs="?",
        default="run",
        choices=["run", "auth", "schedule", "warmup", "fetch", "selfcheck"],
        help="run=full pipeline; auth/schedule/warmup/fetch=partial; selfcheck=offline",
    )
    p.add_argument("--env", default=str(ROOT / ".env"))
    p.add_argument("--at", default=None, help="ISO8601 fire time")
    p.add_argument("--mode", default="normal", help="normal/spam")
    args = p.parse_args(argv)

    if args.stage == "selfcheck":
        self_check()
        return 0

    if args.at:
        fire = _iso_to_ts(args.at)
        left = fire - _now()
        if left > 0:
            print(f"[at] fire at {args.at} (in {left:.0f}s) — sleeping in-process")
            time.sleep(left)

    cfg = Config.load(Path(args.env) if args.env else None)
    return run_pipeline(cfg, args.stage if args.stage != "run" else "run")


if __name__ == "__main__":
    raise SystemExit(main())
