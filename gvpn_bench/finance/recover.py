"""Stop the clients, close their channels, return funds, then allow deletion.

Three phases, each restartable:

``initiate``
    For every client: snapshot its balance, disconnect, stop and mask the
    worker, stop the guards, back up payment state. Then stop the local
    heartbeat. On chain: find every outgoing channel (client list plus
    ``ChannelOpened`` logs since the first funding block) and start closure
    for open ones. Journal: ``state/recovery.json``.

``finalize``
    After the closure grace period: finalize closures, verify every
    channel is closed with zero balance, move Safe and node wxHOPR and xDAI
    back to the funding Safe (keeping a gas reserve in the node, whose key
    is backed up). Audit the returns from receipts and write
    ``state/cleanup.json`` with the balances it saw and a digest of the
    recovery journal.

``verify_for_destroy``
    Re-check everything ``finalize`` recorded against the chain right
    before deletion. Any change refuses deletion.

All keys come from the local identity backups, never from the VMs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

from ..host import Host
from ..profiles import Profile
from ..runs import Run, UserConfig, utc_now, write_private_json
from ..vpn import CtlError
from .chain import WXHOPR_TOKEN, load_chain
from .wallet import SAFE_ABI, Journal, fn, gas_price, require_web3, safe_call, token_contract

log = logging.getLogger("gnosis_speedtest")

CHANNEL_ABI = [
    fn("channels", ("bytes32",), ("uint96", "uint48", "uint32", "uint24", "uint8"), "view"),
    fn("initiateOutgoingChannelClosureSafe", ("address", "address")),
    fn("finalizeOutgoingChannelClosureSafe", ("address", "address")),
]
CHANNEL_CLOSED = 0
CHANNEL_OPEN = 1
CHANNEL_PENDING = 2
GAS_RESERVE_UNITS = 200_000


# -- identities ---------------------------------------------------------------


def node_account(run: Run, client: str) -> Any:
    """Decrypt the client's chain key from its local identity backup."""
    require_web3()
    from eth_account import Account
    from eth_keyfile import decode_keyfile_json

    manifest = json.loads((run.state / "backups.json").read_text())
    files = [Path(p) for p in manifest[client]["identity_files"]]
    id_file = next(f for f in files if f.suffix == ".id")
    pass_file = next(f for f in files if f.suffix == ".pass")
    clear = decode_keyfile_json(json.loads(id_file.read_text()), pass_file.read_bytes().strip())
    return Account.from_key(json.loads(clear)["chain_key"])


# -- phase 1: halt clients ------------------------------------------------------


BALANCE_ATTEMPTS = 3
BALANCE_RETRY_S = 10.0


def read_balance(vpn: Any, name: str, attempts: int = BALANCE_ATTEMPTS, pause_s: float = BALANCE_RETRY_S) -> Any:
    """The worker's ``balance`` answer, retried: a busy worker can fail it once (ctl exit 70)."""
    for attempt in range(1, attempts + 1):
        try:
            balance = vpn.ctl("balance", timeout_s=45).get("Balance", {}).get("Ok")
        except CtlError as error:
            log.warning("%s: balance unavailable (attempt %d of %d): %s", name, attempt, attempts, error)
            balance = None
        if balance is not None:
            return balance
        if attempt < attempts:
            time.sleep(pause_s)
    return None


def halt_client(
    user: UserConfig,
    run: Run,
    profile: Profile,
    host: Host,
    *,
    vm_deleted: bool = False,
) -> dict[str, Any]:
    """Snapshot balance, stop the worker and guards, back up payment state.

    ``vm_deleted`` (the droplet is confirmed gone, e.g. after the deletion
    timer) skips everything on the VM: the balance comes from the last
    snapshot or the backed-up Safe info, and the payment-state backup is
    the last good copy. Recovery then proceeds from local backups only.
    """
    from ..remote.backup import backup_payment_state, manifest
    from ..remote.connect import vpn_for
    from ..remote.guards import stop_guards

    saved = run.state / f"{host.name}-balance-final.json"
    if vm_deleted:
        if not saved.exists():
            info_file = manifest(run).get(host.name, {}).get("safe_info")
            if not info_file:
                raise RuntimeError(f"{host.name}: VM is gone and no Safe info was backed up")
            write_private_json(saved, {"info": json.loads(Path(info_file).read_text()), "channels_out": []})
        log.warning("%s: VM already deleted; recovering from local backups only", host.name)
        return json.loads(saved.read_text())
    vpn = vpn_for(host, profile)
    balance: dict[str, Any] | None = None
    if not saved.exists():
        balance = read_balance(vpn, host.name)
        if balance is None:
            info_file = manifest(run).get(host.name, {}).get("safe_info")
            if not info_file:
                raise RuntimeError(f"{host.name}: no balance and no saved Safe info; cannot recover safely")
            balance = {"info": json.loads(Path(info_file).read_text()), "channels_out": []}
        write_private_json(saved, balance)
    for command in ("disconnect", "stop-client"):
        try:
            response = vpn.ctl(command, timeout_s=45)
            log.info("%s: %s → %s", host.name, command, json.dumps(response)[:200])
        except CtlError as error:
            log.warning("%s: %s: %s", host.name, command, error)
    for argv in (["systemctl", "stop", "gnosisvpn"], ["systemctl", "mask", "gnosisvpn"]):
        result = host.run(argv, timeout_s=90)
        if not result.ok:
            raise RuntimeError(f"{host.name}: {' '.join(argv)} failed: {result.stderr.strip()[:200]}")
    stop_guards(host)
    backup_payment_state(user, run, profile, host)
    return json.loads(saved.read_text())


# -- phase 1b: start channel closure ---------------------------------------------


def _opened_peers(w3: Any, channels: Any, node: str, from_block: int) -> set[str]:
    topic = w3.to_hex(w3.keccak(text="ChannelOpened(bytes32,address,address,bytes32)"))
    logs = w3.eth.get_logs(
        {
            "address": channels.address,
            "fromBlock": from_block,
            "toBlock": "latest",
            "topics": [topic, None, "0x" + node[2:].lower().zfill(64)],
        }
    )
    return {w3.to_checksum_address("0x" + entry["topics"][3].hex()[-40:]) for entry in logs}


def _channel_id(w3: Any, node: str, peer: str) -> bytes:
    return w3.solidity_keccak(["address", "address"], [node, peer])


def first_funding_block(w3: Any, run: Run) -> int:
    journal = Journal(run).entries()
    funding = [e for e in journal if str(e.get("purpose", "")).startswith("fund:") and e["status"] == "confirmed"]
    if not funding:
        raise RuntimeError("no confirmed funding transaction in the journal")
    return int(w3.eth.get_transaction_receipt(funding[0]["hash"])["blockNumber"]) - 10


def initiate(w3: Any, run: Run, profile: Profile, balances: dict[str, dict[str, Any]]) -> dict[str, Any]:
    chain = load_chain(run, profile)
    channels = w3.eth.contract(address=w3.to_checksum_address(chain["contractAddresses"]["channels"]), abi=CHANNEL_ABI)
    journal = Journal(run)
    from_block = first_funding_block(w3, run)
    # A new initiation invalidates any earlier journal and cleanup record.
    (run.state / "cleanup.json").unlink(missing_ok=True)
    write_private_json(run.state / "recovery.json", {})
    result: dict[str, Any] = {}
    for client, balance in sorted(balances.items()):
        owner = node_account(run, client)
        node = w3.to_checksum_address(balance["info"]["node_address"])
        if owner.address != node:
            raise RuntimeError(f"{client}: identity backup does not control node {node}")
        safe = w3.to_checksum_address(balance["info"]["safe_address"])
        peers = {w3.to_checksum_address(c["address"]) for c in balance.get("channels_out", [])}
        peers |= _opened_peers(w3, channels, node, from_block)
        pending = []
        for peer in sorted(peers):
            channel_id = _channel_id(w3, node, peer)
            state = channels.functions.channels(channel_id).call()
            if state[4] == CHANNEL_OPEN:
                data = channels.functions.initiateOutgoingChannelClosureSafe(node, peer)._encode_transaction_data()
                journal.send(w3, owner, f"close:{client}:{peer}", safe_call(w3, safe, owner, channels.address, data))
                state = channels.functions.channels(channel_id).call()
            if state[4] == CHANNEL_PENDING:
                pending.append({"peer": peer, "closure_time": state[2]})
        result[client] = {
            "node": node,
            "safe": safe,
            "pending": pending,
            "network": chain["network"],
            "channels_contract": channels.address,
            "from_block": from_block,
        }
        write_private_json(run.state / "recovery.json", result)
        log.info("%s: %d channels awaiting closure", client, len(pending))
    return result


# -- phase 2: finalize and return funds ------------------------------------------


def load_recovery(run: Run, profile: Profile, clients: list[str]) -> dict[str, Any]:
    chain = load_chain(run, profile)
    result = json.loads((run.state / "recovery.json").read_text())
    if not isinstance(result, dict) or set(result) != set(clients):
        raise ValueError("recovery journal must contain every client of this run")
    expected = chain["contractAddresses"]["channels"].lower()
    for client, entry in result.items():
        if entry.get("network") != chain["network"] or str(entry.get("channels_contract", "")).lower() != expected:
            raise ValueError(f"{client}: recovery journal does not match the pinned network")
        if not isinstance(entry.get("pending"), list) or not isinstance(entry.get("from_block"), int):
            raise ValueError(f"{client}: recovery journal is incomplete")
    return result


def recovery_complete(run: Run, profile: Profile, clients: list[str]) -> bool:
    try:
        load_recovery(run, profile, clients)
    except (ValueError, FileNotFoundError, json.JSONDecodeError):
        return False
    return True


def recovery_digest(run: Run) -> str:
    return hashlib.sha256((run.state / "recovery.json").read_bytes()).hexdigest()


def verify_channels_closed(w3: Any, channels: Any, entry: dict[str, Any]) -> None:
    peers = {c["peer"] for c in entry["pending"]} | _opened_peers(w3, channels, entry["node"], entry["from_block"])
    for peer in peers:
        state = channels.functions.channels(_channel_id(w3, entry["node"], peer)).call()
        if state[4] != CHANNEL_CLOSED or state[0] != 0:
            raise RuntimeError(f"channel {entry['node']} → {peer} is not closed with zero balance")


def finalize(w3: Any, run: Run, profile: Profile, clients: list[str], destination: str) -> dict[str, Any]:
    chain = load_chain(run, profile)
    result = load_recovery(run, profile, clients)
    channels = w3.eth.contract(address=w3.to_checksum_address(chain["contractAddresses"]["channels"]), abi=CHANNEL_ABI)
    token = token_contract(w3)
    journal = Journal(run)
    (run.state / "cleanup.json").unlink(missing_ok=True)
    latest_closure = max((c["closure_time"] for e in result.values() for c in e["pending"]), default=0)
    while w3.eth.get_block("latest")["timestamp"] <= latest_closure + 10:
        log.info("waiting for the channel closure grace period…")
        time.sleep(15)
    for client, entry in sorted(result.items()):
        owner = node_account(run, client)
        if owner.address.lower() != entry["node"].lower():
            raise RuntimeError(f"{client}: identity backup does not match the recovery journal")
        for channel in entry["pending"]:
            channel_id = _channel_id(w3, entry["node"], channel["peer"])
            if channels.functions.channels(channel_id).call()[4] == CHANNEL_PENDING:
                data = channels.functions.finalizeOutgoingChannelClosureSafe(
                    entry["node"], channel["peer"]
                )._encode_transaction_data()
                call = safe_call(w3, entry["safe"], owner, channels.address, data)
                journal.send(w3, owner, f"finalize:{client}:{channel['peer']}", call)
        verify_channels_closed(w3, channels, entry)
        safe_tokens = token.functions.balanceOf(entry["safe"]).call()
        if safe_tokens:
            data = token.functions.transfer(destination, safe_tokens)._encode_transaction_data()
            call = safe_call(w3, entry["safe"], owner, WXHOPR_TOKEN, data)
            journal.send(w3, owner, f"return:{client}:safe-wxhopr", call)
        node_tokens = token.functions.balanceOf(owner.address).call()
        if node_tokens:
            journal.send(w3, owner, f"return:{client}:node-wxhopr", token.functions.transfer(destination, node_tokens))
        safe_native = w3.eth.get_balance(entry["safe"])
        if safe_native:
            call = safe_call(w3, entry["safe"], owner, destination, value=safe_native)
            journal.send(w3, owner, f"return:{client}:safe-xdai", call)
        if token.functions.balanceOf(entry["safe"]).call() or token.functions.balanceOf(owner.address).call():
            raise RuntimeError(f"{client}: wxHOPR remains after the return transfers")
        remainder = w3.eth.get_balance(owner.address)
        reserve = GAS_RESERVE_UNITS * gas_price(w3)
        if remainder > reserve:
            journal.send(w3, owner, f"return:{client}:node-xdai", to=destination, value=remainder - reserve)
    tokens, native = audit_returns(w3, run, result, destination)
    from ..remote.inventory import load_inventory

    cleanup = {
        "recovered_wxHOPR": str(w3.from_wei(tokens, "ether")),
        "recovered_xDAI": str(w3.from_wei(native, "ether")),
        "destination": destination,
        "network": chain["network"],
        "channels_contract": channels.address,
        "clients": sorted(clients),
        "droplet_ids": sorted(m.id for m in load_inventory(run)),
        "all_channels_closed": True,
        "safe_token_balances_zero": True,
        "node_native_balances": {c: w3.eth.get_balance(e["node"]) for c, e in result.items()},
        "recovery_digest": recovery_digest(run),
        "finalized_at": utc_now(),
    }
    write_private_json(run.state / "cleanup.json", cleanup)
    return cleanup


def audit_returns(w3: Any, run: Run, result: dict[str, Any], destination: str) -> tuple[int, int]:
    """Sum wxHOPR and xDAI that reached *destination*, from receipts only.

    Receipts (not this process's counters) include returns completed by an
    earlier, interrupted invocation.
    """
    owners = {e["node"].lower() for e in result.values()}
    sources = owners | {e["safe"].lower() for e in result.values()}
    safes = {e["safe"].lower() for e in result.values()}
    transfer_topic = w3.keccak(text="Transfer(address,address,uint256)")
    tokens = 0
    native = 0
    for entry in Journal(run).entries():
        if entry["sender"].lower() not in owners or entry["status"] != "confirmed":
            continue
        receipt = w3.eth.get_transaction_receipt(entry["hash"])
        for event in receipt["logs"]:
            topics = event["topics"]
            if event["address"].lower() != WXHOPR_TOKEN or len(topics) != 3 or topics[0] != transfer_topic:
                continue
            source = "0x" + topics[1].hex()[-40:]
            target = "0x" + topics[2].hex()[-40:]
            if source.lower() in sources and target.lower() == destination.lower():
                tokens += int.from_bytes(event["data"], "big")
        tx = w3.eth.get_transaction(entry["hash"])
        if tx["to"].lower() == destination.lower():
            native += tx["value"]
        elif tx["to"].lower() in safes:
            safe = w3.eth.contract(address=tx["to"], abi=SAFE_ABI)
            _, args = safe.decode_function_input(tx["input"])
            if args["a0"].lower() == destination.lower():
                native += args["a1"]
    return tokens, native


# -- phase 3: verify before deleting VMs -------------------------------------------


def verify_for_destroy(w3: Any, run: Run, profile: Profile, clients: list[str], droplet_ids: list[int]) -> None:
    """Raise unless the chain still matches what ``finalize`` recorded."""
    cleanup_file = run.state / "cleanup.json"
    if not cleanup_file.exists():
        raise RuntimeError("recovery has not finished (no cleanup.json); refusing to delete VMs")
    cleanup = json.loads(cleanup_file.read_text())
    chain = load_chain(run, profile)
    result = load_recovery(run, profile, clients)
    checks = {
        "clients": cleanup.get("clients") == sorted(clients),
        "droplets": cleanup.get("droplet_ids") == sorted(droplet_ids),
        "journal digest": cleanup.get("recovery_digest") == recovery_digest(run),
        "network": cleanup.get("network") == chain["network"],
        "channels": cleanup.get("all_channels_closed") is True,
        "safes": cleanup.get("safe_token_balances_zero") is True,
    }
    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        raise RuntimeError(f"cleanup record does not match this run ({', '.join(failed)}); refusing to delete")
    channels = w3.eth.contract(address=w3.to_checksum_address(chain["contractAddresses"]["channels"]), abi=CHANNEL_ABI)
    token = token_contract(w3)
    for client, entry in result.items():
        if node_account(run, client).address.lower() != entry["node"].lower():
            raise RuntimeError(f"{client}: identity backup no longer matches; refusing to delete")
        verify_channels_closed(w3, channels, entry)
        if (
            token.functions.balanceOf(entry["safe"]).call()
            or token.functions.balanceOf(entry["node"]).call()
            or w3.eth.get_balance(entry["safe"])
            or w3.eth.get_balance(entry["node"]) != cleanup["node_native_balances"].get(client)
        ):
            raise RuntimeError(f"{client}: balances changed since recovery; refusing to delete")
