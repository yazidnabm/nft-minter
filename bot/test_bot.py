import sys
sys.path.insert(0, "/home/ubuntu/fcfs-minter/bot")

from bot import Bot, CHAIN_PRESETS, Store
from pathlib import Path

def demo():
    # Store round-trip
    import tempfile, json
    d = tempfile.mkdtemp()
    s = Store(Path(d) / "t.json", {"a": 1})
    s.save({"a": 2, "b": [1, 2]})
    assert s.load() == {"a": 2, "b": [1, 2]}, s.load()

    # env builder (fake bot, no token)
    b = Bot(Path(d) / "c.json")
    b.chain = "robinhood"
    b.cfg["private_key"] = ""
    env = b.build_env({"slug": "robingangz", "qty": "2", "contract": "0x" + "d" * 40})
    assert "COLLECTION_SLUG=robingangz" in env
    assert "CHAIN=robinhood" in env
    assert "RPC_URL=https://rpc.mainnet.chain.robinhood.com" in env
    assert "MAX_FEE_GWEI=0.1" in env
    assert "LIVE=1" in env
    assert "SIWE_CHAIN_ID=4663" in env
    assert "PRIVATE_KEY=" not in env

    # summary
    st = {"flow": "mint", "slug": "robingangz", "qty": "1", "contract": "0x" + "d" * 40}
    assert "robingangz" in b._summary(st)

    # chain presets sanity
    assert CHAIN_PRESETS["base"]["siwe_chain_id"] == 8453
    assert CHAIN_PRESETS["robinhood"]["siwe_chain_id"] == 4663
    print("demo OK")

if __name__ == "__main__":
    demo()
