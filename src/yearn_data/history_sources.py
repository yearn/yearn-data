"""Bounded historical source adapters. No adapter advances a legacy cursor."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re

from eth_utils import event_abi_to_log_topic
from web3 import Web3
from web3._utils.events import get_event_data

from . import abis, envio
from .config import CHAINS, DEFAULT_CHAINS, V2_REGISTRIES_BY_CHAIN, V3_ROLE_MANAGERS
from .coverage import Scope, fingerprint, insert_checked
from .indexing import (
    _hex, normalize_strategy_report,
)

ENVIO_CHAINS = frozenset(CHAINS[key].chain_id for key in DEFAULT_CHAINS)


class HistorySourceError(ValueError):
    """Safe diagnostic containing only adapter-owned messages and public scope."""


def route(chain_id: int, family: str, override: str = "auto") -> str:
    if override not in {"auto", "rpc", "envio"}:
        raise HistorySourceError("invalid source override")
    supported = chain_id in ENVIO_CHAINS and (family == "reports" or family.startswith("inventory:"))
    if override == "envio" and not supported:
        raise HistorySourceError(f"Envio is not verified for chain {chain_id}, family {family}")
    return "envio" if supported and override != "rpc" else "rpc"


def event(name, fields):
    return {"type": "event", "name": name, "anonymous": False,
            "inputs": [{"name": key, "type": kind, "indexed": indexed} for key, kind, indexed in fields]}


EXPERIMENTAL = event("NewExperimentalVault", [
    ("token", "address", True), ("deployer", "address", True),
    ("vault", "address", False), ("api_version", "string", False),
])
ADDED = event("AddedNewVault", [("vault", "address", True), ("debtAllocator", "address", True), ("category", "uint256", False)])
REMOVED = event("RemovedVault", [("vault", "address", True)])


@dataclass(frozen=True)
class InventoryTarget:
    chain_id: int
    address: str
    version: str
    entity: str
    kind: str
    fields: str
    address_field: str
    abi: dict
    action: str = "added"

    @property
    def scope(self):
        # Earlier Envio scans queried only lowercase addresses and could certify
        # empty ranges when the producer stored checksummed emitter addresses.
        return Scope(self.chain_id, self.address, f"inventory:{self.entity}", "address-forms-v2")


def inventory_targets(chain: str, experimental: bool = False):
    cid = CHAINS[chain].chain_id
    entities = envio.V2_VAULT_INVENTORY_ENTITIES
    for address in V2_REGISTRIES_BY_CHAIN.get(cid, ()):
        for index, abi in [(0, abis.V2_REGISTRY_ABI[0]), (2, abis.V2_REGISTRY_ABI[1]), (1, EXPERIMENTAL)]:
            if index == 1 and not experimental:
                continue
            entity, kind, fields, _, address_field, _, action = entities[index]
            yield InventoryTarget(cid, address, "v2", entity, kind, fields, address_field, abi, action)
    if manager := V3_ROLE_MANAGERS.get(cid):
        for spec, abi in zip(envio.V3_VAULT_INVENTORY_ENTITIES[2:], (ADDED, REMOVED)):
            entity, kind, fields, _, address_field, _, action = spec
            yield InventoryTarget(cid, manager, "v3", entity, kind, fields, address_field, abi, action)


def report_scope(vault):
    return Scope(vault["chain_id"], vault["address"], "reports",
                 fingerprint(["address-forms-v2", vault["version"], vault["asset"].lower(), vault["asset_decimals"]]))


class HistoricalSource:
    """One chain client; upper bounds must be pinned before fetching any ranges."""

    def __init__(self, chain, w3, gql=None):
        self.chain = chain
        self.chain_id = CHAINS[chain].chain_id
        self.w3 = w3
        self.gql = gql or envio._gql
        self.headers = {}

    def block(self, number):
        if number not in self.headers:
            block = self.w3.eth.get_block(number)
            if int(block["number"]) != number:
                raise HistorySourceError("RPC returned wrong block")
            self.headers[number] = block
        return self.headers[number]

    def verify_pin(self, pin):
        if int(self.w3.eth.chain_id) != self.chain_id:
            raise HistorySourceError("RPC chain mismatch")
        if _hex(self.w3.eth.get_block(pin["number"])["hash"]).lower() != pin["hash"].lower():
            raise HistorySourceError("pinned block hash changed")

    def pin(self, *, end=None, confirmations=None, use_envio=True):
        if int(self.w3.eth.chain_id) != self.chain_id:
            raise HistorySourceError("RPC chain mismatch")
        # Some retired chains do not implement finalized. The operator must choose
        # a confirmation policy explicitly; never silently fall back to latest.
        if confirmations is None:
            ceiling = int(self.w3.eth.get_block("finalized")["number"])
            policy = "finalized"
        else:
            if confirmations < 1:
                raise HistorySourceError("confirmations must be positive")
            ceiling = max(0, int(self.w3.eth.block_number) - confirmations)
            policy = f"confirmations:{confirmations}"
        if end is not None and (end < 0 or end > ceiling):
            raise HistorySourceError("requested end exceeds safe RPC head")
        number = ceiling if end is None else end
        if use_envio:
            data = self.gql("query($chain: Int!) { chain_metadata(where: {chain_id: {_eq: $chain}}) { latest_processed_block } }", {"chain": self.chain_id})
            rows = data.get("chain_metadata") or []
            if len(rows) != 1:
                raise HistorySourceError("Envio watermark missing or ambiguous")
            watermark = int(rows[0]["latest_processed_block"])
            if end is not None and end > watermark:
                raise HistorySourceError("requested end exceeds Envio watermark")
            number = min(number, watermark)
        block = self.block(number)
        return {"number": number, "hash": _hex(block["hash"]), "timestamp": int(block["timestamp"]), "finality": policy}

    def envio_nodes(self, entity, fields, address_field, address, lo, hi):
        query = envio._page_query(entity, fields + " blockHash", self.chain_id, lo, hi, address_field)
        cursor = (lo, -1)
        while True:
            data = self.gql(query, {"lb": cursor[0], "li": cursor[1], "n": 1000,
                                    "addresses": list(dict.fromkeys([address.lower(), Web3.to_checksum_address(address)]))})
            if entity not in data or not isinstance(data[entity], list):
                raise HistorySourceError("missing Envio entity response")
            nodes = data[entity]
            for node in nodes:
                position = int(node["blockNumber"]), int(node["logIndex"])
                if position <= cursor or position[1] < 0 or not lo <= position[0] <= hi:
                    raise HistorySourceError("Envio pagination or range mismatch")
                if not re.fullmatch(r"0x[0-9a-fA-F]{64}", node["transactionHash"]):
                    raise HistorySourceError("invalid Envio transaction hash")
                if int(node["chainId"]) != self.chain_id or node[address_field].lower() != address.lower():
                    raise HistorySourceError("Envio scope mismatch")
                block = self.block(position[0])
                if node["blockHash"].lower() != _hex(block["hash"]).lower() or int(node["blockTimestamp"]) != int(block["timestamp"]):
                    raise HistorySourceError("Envio block evidence mismatch")
                cursor = position
                yield node
            if len(nodes) < 1000:
                break

    def rpc_logs(self, address, event_abis, lo, hi, topics_tail=()):
        decoders = {_hex(event_abi_to_log_topic(abi)).lower(): abi for abi in event_abis}
        logs = self.w3.eth.get_logs({"address": Web3.to_checksum_address(address),
                                     "fromBlock": lo, "toBlock": hi,
                                     "topics": [list(decoders), *topics_tail]})
        if len(logs) >= 10_000:
            raise HistorySourceError("possible RPC result cap; retry with a smaller chunk size")
        seen = set()
        for log in sorted(logs, key=lambda item: (int(item["blockNumber"]), int(item["logIndex"]))):
            number = int(log["blockNumber"])
            if log.get("removed") or log["address"].lower() != address.lower() or not lo <= number <= hi:
                raise HistorySourceError("RPC log scope mismatch")
            block = self.block(number)
            if _hex(log["blockHash"]).lower() != _hex(block["hash"]).lower():
                raise HistorySourceError("RPC log block hash mismatch")
            key = _hex(log["transactionHash"]), int(log["logIndex"])
            if key[1] < 0 or not re.fullmatch(r"0x[0-9a-fA-F]{64}", key[0]):
                raise HistorySourceError("invalid RPC log identity")
            if key in seen:
                raise HistorySourceError("duplicate RPC log identity")
            seen.add(key)
            abi = decoders[_hex(log["topics"][0]).lower()]
            decoded = get_event_data(self.w3.codec, abi, log)
            yield log, int(block["timestamp"]), dict(decoded["args"]), abi["name"]

    def inventory(self, target, lo, hi, source):
        if source == "envio":
            observations = ((envio._log(node), int(node["blockTimestamp"]), node) for node in
                            self.envio_nodes(target.entity, target.fields, target.address_field, target.address, lo, hi))
        else:
            observations = ((log, ts, args) for log, ts, args, _ in self.rpc_logs(target.address, [target.abi], lo, hi))
        rows = []
        for log, ts, args in observations:
            # Keep one canonical representation across RPC and Envio.
            payload = {field["name"]: args.get(field["name"], args.get("deployment_id"))
                       for field in target.abi["inputs"]}
            for field in target.abi["inputs"]:
                key = field["name"]
                if field["type"].startswith("uint"):
                    payload[key] = int(payload[key])
                elif field["type"] == "address":
                    payload[key] = Web3.to_checksum_address(payload[key])
            rows.append(dict(chain_id=self.chain_id, version=target.version,
                             vault_address=Web3.to_checksum_address(args["vault"]), source_kind=target.kind,
                             action=target.action, source_address=Web3.to_checksum_address(target.address),
                             asset=Web3.to_checksum_address(args["token"]) if args.get("token") else None,
                             tx_hash=_hex(log["transactionHash"]), log_index=int(log["logIndex"]),
                             block_number=int(log["blockNumber"]), block_timestamp=ts,
                             decoded_json=json.dumps(payload, sort_keys=True)))
        return rows

    def reports(self, vault, lo, hi, source):
        version = vault["version"]
        if source == "envio":
            entity, fields, decode = ("StrategyReported", envio.V3_REPORT_FIELDS, envio._v3_report_args) if version == "v3" else ("V2StrategyReported", envio.V2_REPORT_FIELDS, envio._v2_report_args)
            events = ((envio._log(n), int(n["blockTimestamp"]), decode(n), "StrategyReported") for n in
                      self.envio_nodes(entity, fields, "vaultAddress", vault["address"], lo, hi))
        else:
            event_abis = [abis.V3_STRATEGY_REPORTED_EVENT] if version == "v3" else abis.V2_STRATEGY_REPORTED_EVENTS
            events = self.rpc_logs(vault["address"], event_abis, lo, hi)
        rows = []
        for log, ts, args, _ in events:
            if version == "v2":
                args.setdefault("debtPaid", 0)
            rows.append(normalize_strategy_report(self.chain_id, version, vault["address"], vault["asset"], vault["asset_decimals"], log, ts, args))
        return rows



def write_rows(conn, table, rows):
    columns = [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]
    identity = ("chain_id", "tx_hash", "log_index")
    if table == "vault_inventory_events":
        identity += ("source_kind",)
    count = 0
    for row in rows:
        values = row if isinstance(row, dict) else dict(zip(columns, row))
        if table == "strategy_reports" and values["version"] == "v2":
            old = conn.execute("SELECT extra_json FROM strategy_reports WHERE chain_id=? AND tx_hash=? AND log_index=?",
                               tuple(values[key] for key in identity)).fetchone()
            if old:
                prior, current = json.loads(old[0]), json.loads(values["extra_json"])
                if {**prior, "debtPaid": prior.get("debtPaid", 0)} == {**current, "debtPaid": current.get("debtPaid", 0)}:
                    values = {**values, "extra_json": old[0]}
        if table == "vault_inventory_events":
            old = conn.execute(
                "SELECT decoded_json FROM vault_inventory_events WHERE chain_id=? AND tx_hash=? AND log_index=? AND source_kind=?",
                tuple(values[key] for key in identity),
            ).fetchone()
            if old:
                # Legacy Envio rows retain the full node. Compare its event
                # arguments to the canonical payload and preserve that evidence.
                previous = json.loads(old[0])
                expected = json.loads(values["decoded_json"])
                for key, value in expected.items():
                    prior = previous.get(key, previous.get("deployment_id") if key == "vault_id" else None)
                    if isinstance(value, int) and prior is not None:
                        prior = int(prior)
                    if isinstance(value, str) and value.startswith("0x") and isinstance(prior, str):
                        prior, value = prior.lower(), value.lower()
                    if prior != value:
                        raise HistorySourceError("conflicting inventory arguments")
                values = {**values, "decoded_json": old[0]}
        enrich = ("asset", "asset_decimals") if table == "strategy_reports" else ()
        count += insert_checked(conn, table, values, identity, enrich=enrich)
        raw_args = None
        if table == "strategy_reports":
            raw_args = json.loads(values["extra_json"])
            raw_args.update(strategy=values["strategy_address"], gain=int(values["gain_raw"]), loss=int(values["loss_raw"]))
            if values["version"] == "v3":
                for field in ("current_debt", "protocol_fees", "total_fees", "total_refunds"):
                    raw_args[field] = int(values[field + "_raw"])
            name = "StrategyReported"
        if raw_args is not None:
            insert_checked(conn, "events_raw", dict(
                chain_id=values["chain_id"], contract_address=values["vault_address"], event_name=name,
                tx_hash=values["tx_hash"], log_index=values["log_index"], block_number=values["block_number"],
                block_timestamp=values["block_timestamp"], decoded_json=json.dumps(raw_args, sort_keys=True)),
                ("chain_id", "tx_hash", "log_index"))
    return count
