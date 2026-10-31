"""Funding Safe access and a transaction journal that refuses ambiguity.

The funding key never leaves this process. Every transaction is appended
to ``state/transactions.jsonl`` *before* waiting for its receipt::

    {"hash", "sender", "nonce", "purpose", "status", "time"}

``status`` is pending, confirmed, reverted or dropped. Before sending,
every pending entry is reconciled against its receipt and the chain's
nonce. An entry without a receipt whose nonce is still unused and that
the node does not know is ``dropped`` (never broadcast): the next
transaction reuses its nonce, so at most one of them can execute.
Anything else without a receipt stops all sending, because retrying an
ambiguous transaction could pay twice.

web3 is imported lazily (``pip install -e '.[remote]'``).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..runs import Run, read_secret
from .chain import WXHOPR_TOKEN

RPC_URL = "https://rpc.gnosischain.com"
ZERO_ADDRESS = "0x" + "0" * 40
RECEIPT_TIMEOUT_S = 180


class AmbiguousTransactionError(RuntimeError):
    """A sent transaction has no receipt yet; reconcile before sending again."""


def require_web3() -> Any:
    try:
        import web3
    except ImportError:
        raise SystemExit("funding and recovery need web3: pip install -e '.[remote]'") from None
    return web3


def fn(
    name: str, inputs: tuple[str, ...] = (), outputs: tuple[str, ...] = (), mutability: str = "nonpayable"
):
    return {
        "type": "function",
        "name": name,
        "stateMutability": mutability,
        "inputs": [{"name": f"a{i}", "type": t} for i, t in enumerate(inputs)],
        "outputs": [{"name": f"r{i}", "type": t} for i, t in enumerate(outputs)],
    }


SAFE_ABI = [
    fn("getOwners", outputs=("address[]",), mutability="view"),
    fn("getThreshold", outputs=("uint256",), mutability="view"),
    fn(
        "execTransaction",
        (
            "address",
            "uint256",
            "bytes",
            "uint8",
            "uint256",
            "uint256",
            "uint256",
            "address",
            "address",
            "bytes",
        ),
        ("bool",),
    ),
]
TOKEN_ABI = [
    fn("balanceOf", ("address",), ("uint256",), "view"),
    fn("transfer", ("address", "uint256"), ("bool",)),
]


@dataclass(frozen=True)
class FundingSource:
    account: Any  # eth_account LocalAccount
    safe: str


def parse_key_file(text: str) -> dict[str, str]:
    """``private_key: …``, ``eoa: …``, ``safe: …`` lines (the existing ~/key.txt format)."""
    fields = dict(re.findall(r"^(\w+):\s*(\S+)", text, re.M))
    for key in ("private_key", "eoa", "safe"):
        if key not in fields:
            raise ValueError(f"funding key file has no {key!r} line")
    return fields


def load_funding_source(path: Path) -> FundingSource:
    web3 = require_web3()
    from eth_account import Account

    fields = parse_key_file(read_secret(path))
    account = Account.from_key(fields["private_key"])
    if account.address.lower() != fields["eoa"].lower():
        raise RuntimeError("funding key does not match the stated EOA")
    return FundingSource(account=account, safe=web3.Web3.to_checksum_address(fields["safe"]))


def connect_chain(rpc_url: str = RPC_URL) -> Any:
    web3 = require_web3()
    from web3.middleware import ExtraDataToPOAMiddleware

    w3 = web3.Web3(web3.Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 45}))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    if w3.eth.chain_id != 100:
        raise RuntimeError("RPC is not Gnosis Chain")
    return w3


def token_contract(w3: Any) -> Any:
    return w3.eth.contract(address=w3.to_checksum_address(WXHOPR_TOKEN), abi=TOKEN_ABI)


def gas_price(w3: Any) -> int:
    return max(1_000_000, w3.eth.gas_price * 3)


def safe_call(w3: Any, safe_address: str, owner: Any, target: str, data: bytes = b"", value: int = 0) -> Any:
    """Build a 1-of-1 Safe execTransaction and simulate it before returning it."""
    safe = w3.eth.contract(address=w3.to_checksum_address(safe_address), abi=SAFE_ABI)
    if safe.functions.getThreshold().call() != 1 or owner.address not in safe.functions.getOwners().call():
        raise RuntimeError(f"Safe {safe_address} is not controlled by the supplied single owner")
    # Pre-validated signature for the msg.sender owner (v = 1).
    signature = bytes.fromhex(owner.address[2:].zfill(64)) + bytes(32) + b"\x01"
    call = safe.functions.execTransaction(
        w3.to_checksum_address(target), value, data, 0, 0, 0, 0, ZERO_ADDRESS, ZERO_ADDRESS, signature
    )
    if call.call({"from": owner.address}) is not True:
        raise RuntimeError("Safe transaction simulation failed")
    return call


def _known_to_node(w3: Any, tx_hash: str) -> bool:
    try:
        return w3.eth.get_transaction(tx_hash) is not None
    except Exception:  # web3 raises TransactionNotFound
        return False


class Journal:
    """Append-only transaction ledger with receipt/nonce reconciliation."""

    def __init__(self, run: Run) -> None:
        self.path = run.state / "transactions.jsonl"

    def entries(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]

    def _rewrite(self, entries: list[dict[str, Any]]) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text("".join(json.dumps(e) + "\n" for e in entries))
        tmp.chmod(0o600)
        tmp.replace(self.path)

    def append(self, entry: dict[str, Any]) -> None:
        with self.path.open("a") as stream:
            stream.write(json.dumps(entry) + "\n")
        self.path.chmod(0o600)

    def reconcile(self, w3: Any) -> None:
        """Resolve pending entries from receipts; raise if any stays ambiguous."""
        entries = self.entries()
        unresolved = []
        for entry in entries:
            if entry["status"] not in ("pending", "dropped"):
                continue
            try:
                receipt = w3.eth.get_transaction_receipt(entry["hash"])
            except Exception:  # web3 raises TransactionNotFound
                receipt = None
            if receipt is not None:
                entry["status"] = "confirmed" if receipt["status"] == 1 else "reverted"
                entry["block"] = receipt["blockNumber"]
                continue
            if entry["status"] == "dropped":
                continue
            confirmed_nonce = w3.eth.get_transaction_count(entry["sender"], "latest")
            if confirmed_nonce <= entry["nonce"] and not _known_to_node(w3, entry["hash"]):
                # Never broadcast (crash between journaling and sending) or
                # dropped. Its nonce is still free, so the next transaction
                # from this sender reuses it and makes this one invalid: it
                # can no longer execute, and retrying cannot pay twice.
                entry["status"] = "dropped"
                continue
            reason = (
                "nonce used by another transaction"
                if confirmed_nonce > entry["nonce"]
                else "still in the mempool"
            )
            unresolved.append(f"{entry['hash']} ({entry.get('purpose')}, nonce {entry['nonce']}: {reason})")
        self._rewrite(entries)
        if unresolved:
            raise AmbiguousTransactionError(
                "unresolved transactions; check them on a block explorer before retrying: "
                + "; ".join(unresolved)
            )

    def confirmed(self, purpose: str) -> bool:
        return any(e.get("purpose") == purpose and e["status"] == "confirmed" for e in self.entries())

    def send(
        self, w3: Any, account: Any, purpose: str, call: Any = None, to: str | None = None, value: int = 0
    ):
        """Sign, journal, broadcast, wait. Raises on revert or ambiguity."""
        self.reconcile(w3)
        base = {
            "from": account.address,
            "nonce": w3.eth.get_transaction_count(account.address, "pending"),
            "chainId": 100,
            "gasPrice": gas_price(w3),
        }
        if call is not None:
            gas = call.estimate_gas({"from": account.address})
            tx = call.build_transaction({**base, "gas": max(750_000, int(gas * 2))})
        else:
            if to is None:
                raise ValueError("send needs a call or a recipient")
            tx = {**base, "to": w3.to_checksum_address(to), "value": value}
            tx["gas"] = max(50_000, int(w3.eth.estimate_gas(tx) * 1.5))
            w3.eth.call(tx)
        signed = account.sign_transaction(tx)
        tx_hash = w3.keccak(signed.raw_transaction).hex()
        if not tx_hash.startswith("0x"):
            tx_hash = "0x" + tx_hash
        # Journal first: a crash after broadcast still leaves a trace to reconcile.
        self.append(
            {
                "hash": tx_hash,
                "sender": account.address,
                "nonce": tx["nonce"],
                "purpose": purpose,
                "status": "pending",
                "time": time.time(),
            }
        )
        w3.eth.send_raw_transaction(signed.raw_transaction)
        try:
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=RECEIPT_TIMEOUT_S)
        except Exception as error:
            raise AmbiguousTransactionError(f"{purpose}: no receipt for {tx_hash}: {error}") from error
        self.reconcile(w3)
        if receipt["status"] != 1:
            raise RuntimeError(f"{purpose}: transaction reverted: {tx_hash}")
        return tx_hash


def describe_source(w3: Any, source: FundingSource) -> dict[str, Any]:
    safe = w3.eth.contract(address=source.safe, abi=SAFE_ABI)
    token = token_contract(w3)
    return {
        "safe": source.safe,
        "threshold": safe.functions.getThreshold().call(),
        "key_is_owner": source.account.address in safe.functions.getOwners().call(),
        "safe_wxhopr": str(w3.from_wei(token.functions.balanceOf(source.safe).call(), "ether")),
        "safe_xdai": str(w3.from_wei(w3.eth.get_balance(source.safe), "ether")),
        "owner_gas_xdai": str(w3.from_wei(w3.eth.get_balance(source.account.address), "ether")),
    }


def fund_nodes(w3: Any, run: Run, source: FundingSource, nodes: list[dict[str, str]]) -> None:
    """Send each node its recommended wxHOPR and xDAI from the funding Safe.

    Idempotent per (node, asset): a confirmed journal entry is skipped. A
    node that already holds funds without a matching journal entry is
    refused: something else funded it and we must not fund it twice.
    """
    journal = Journal(run)
    journal.reconcile(w3)
    token = token_contract(w3)
    for node in nodes:
        address = w3.to_checksum_address(node["address"])
        token_purpose = f"fund:{node['name']}:wxhopr"
        native_purpose = f"fund:{node['name']}:xdai"
        has_tokens = token.functions.balanceOf(address).call() > 0
        has_native = w3.eth.get_balance(address) > 0
        if (has_tokens and not journal.confirmed(token_purpose)) or (
            has_native and not journal.confirmed(native_purpose)
        ):
            raise RuntimeError(f"{node['name']} already holds funds this run did not send; reconcile first")
        if not journal.confirmed(token_purpose):
            data = token.functions.transfer(
                address, w3.to_wei(node["wxhopr"], "ether")
            )._encode_transaction_data()
            journal.send(
                w3,
                source.account,
                token_purpose,
                safe_call(w3, source.safe, source.account, WXHOPR_TOKEN, data),
            )
        if not journal.confirmed(native_purpose):
            value = w3.to_wei(node["xdai"], "ether")
            call = safe_call(w3, source.safe, source.account, address, value=value)
            journal.send(w3, source.account, native_purpose, call)
