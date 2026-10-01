import sys
import json
import eth_abi
from web3 import Web3

def run():
    env_file = sys.argv[1]
    env = {}
    with open(env_file, 'r') as f:
        for line in f:
            if '=' in line:
                k, v = line.strip().split('=', 1)
                env[k] = v

    rpc = env.get("RPC_URL")
    contract = env.get("NFT_CONTRACT")
    qty = int(env.get("QUANTITY", 1))
    chain_id = int(env.get("SIWE_CHAIN_ID", 4663))
    gas_mode = env.get("GAS_MODE", "legacy")
    pk = env.get("PRIVATE_KEY", "")
    
    if not pk:
        import os
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(base_dir, "wallets.txt")) as f:
            for l in f:
                if not l.startswith("#") and l.strip():
                    pk = l.strip()
                    break

    if not pk.startswith("0x"): pk = "0x" + pk

    print(f"[swap] SPAM MODE: Bypassed GQL, constructing direct SeaDrop Tx...")
    w3 = Web3(Web3.HTTPProvider(rpc))
    acct = w3.eth.account.from_key(pk)
    
    seadrop_addr = Web3.to_checksum_address("0x00005EA00Ac477B1030CE78506496e8C2dE24bf5")
    fee_recipient = Web3.to_checksum_address("0x0000a26b00c1F0DF003000390027140000fAa719")
    minter_not_payer = Web3.to_checksum_address("0x0000000000000000000000000000000000000000")
    
    encoded = eth_abi.encode(["address", "address", "address", "uint256"], [Web3.to_checksum_address(contract), fee_recipient, minter_not_payer, qty])
    calldata = "0x161ac21f" + encoded.hex()
    
    tx = {
        "to": seadrop_addr,
        "data": calldata,
        "value": w3.to_wei(0.0001, "ether") * qty, # HARDCODE harga < 0.0001 ETH untuk lambithood (atau ganti jadi dinamis)
        "chainId": chain_id,
        "from": acct.address,
        "nonce": w3.eth.get_transaction_count(acct.address)
    }
    
    if gas_mode == "legacy":
        tx["gasPrice"] = w3.to_wei(float(env.get("MAX_FEE_GWEI", 0.1)), "gwei")
    else:
        tx["maxFeePerGas"] = w3.to_wei(float(env.get("MAX_FEE_GWEI", 0.1)), "gwei")
        tx["maxPriorityFeePerGas"] = w3.to_wei(float(env.get("MAX_PRIORITY_GWEI", 0.01)), "gwei")
        
    try:
        tx["gas"] = int(w3.eth.estimate_gas(tx) * 1.2)
    except Exception as e:
        tx["gas"] = int(env.get("GAS_LIMIT", 150000))
        print(f"[tx] gas estimation failed, using limit {tx['gas']}: {e}")

    if env.get("LIVE") == "1":
        print(f"[tx] Broadcasting to {rpc}...")
        signed = w3.eth.account.sign_transaction(tx, pk)
        try:
            h = w3.eth.send_raw_transaction(signed.raw_transaction)
            print(f"[tx] broadcast OK: {h.hex()}")
        except Exception as e:
            print(f"ERROR: broadcast failed: {e}")
            sys.exit(1)
    else:
        print(f"[tx] DRY RUN: would send tx")

    print("[done] Spam mode complete")

if __name__ == "__main__":
    run()
