#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MixIT — Music sharing and trading app for DJ remixes and collaboration.
Bitfinex-style flows: list stems, place bids, fill offers, manage collabs and royalties.
Single-file app; connects to MixFinex-style EVM contracts.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import struct
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

# -----------------------------------------------------------------------------
# EIP-55 checksum for addresses (40 hex after 0x)
# ------------------------------------------------------------------------------

def _keccak256(data: bytes) -> bytes:
    try:
        from Crypto.Hash import keccak
        k = keccak.new(digest_bits=256)
        k.update(data)
        return k.digest()
    except Exception:
        try:
            import sha3
            return sha3.keccak_256(data).digest()
        except Exception:
            return hashlib.sha3_256(data).digest()


def to_checksum_address(addr: str) -> str:
    addr = addr.lower().replace("0x", "")
    if len(addr) != 40:
        return "0x" + addr
    h = _keccak256(addr.encode("ascii")).hex()
    out = "0x"
    for i, c in enumerate(addr):
        nibble = int(h[i], 16)
        if nibble >= 8 and c in "abcdef":
            out += c.upper()
        else:
            out += c
    return out


def random_address_eip55() -> str:
    raw = "0x" + secrets.token_hex(20)
    return to_checksum_address(raw)


# -----------------------------------------------------------------------------
# Constants and config
# ------------------------------------------------------------------------------

class MixITConstants:
    APP_NAME = "MixIT"
    VERSION = "1.0.0"
    CONFIG_DIR = ".mixit"
    CONFIG_FILE = "config.json"
    DEFAULT_RPC = "https://eth.llamarpc.com"
    DEFAULT_CHAIN_ID = 1
    BPS_DENOM = 10000
    MAX_FEE_BPS = 450
    STEM_STATUS_UNKNOWN = 0
    STEM_STATUS_FILLED = 1
    STEM_STATUS_DELISTED = 2
    STEM_STATUS_EXPIRED = 3
    STEM_STATUS_ACTIVE = 4
    BID_STATUS_UNKNOWN = 0
    BID_STATUS_FILLED = 1
    BID_STATUS_CANCELLED = 2
    BID_STATUS_EXPIRED = 3
    BID_STATUS_ACTIVE = 4


@dataclass
class MixITConfig:
    rpc_url: str = MixITConstants.DEFAULT_RPC
    chain_id: int = MixITConstants.DEFAULT_CHAIN_ID
    contract_address: Optional[str] = None
    private_key: Optional[str] = None
    treasury: Optional[str] = None
    fee_vault: Optional[str] = None
    default_gas_limit: int = 300_000
    default_gas_price_gwei: float = 30.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rpc_url": self.rpc_url,
            "chain_id": self.chain_id,
            "contract_address": self.contract_address,
            "treasury": self.treasury,
            "fee_vault": self.fee_vault,
            "default_gas_limit": self.default_gas_limit,
            "default_gas_price_gwei": self.default_gas_price_gwei,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "MixITConfig":
        return cls(
            rpc_url=d.get("rpc_url", MixITConstants.DEFAULT_RPC),
            chain_id=int(d.get("chain_id", MixITConstants.DEFAULT_CHAIN_ID)),
            contract_address=d.get("contract_address"),
            private_key=d.get("private_key"),
            treasury=d.get("treasury"),
            fee_vault=d.get("fee_vault"),
            default_gas_limit=int(d.get("default_gas_limit", 300_000)),
            default_gas_price_gwei=float(d.get("default_gas_price_gwei", 30.0)),
        )

    def save(self, path: Optional[Path] = None) -> None:
        path = path or Path.home() / MixITConstants.CONFIG_DIR / MixITConstants.CONFIG_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        data = self.to_dict()
        if self.private_key:
            data["private_key"] = self.private_key
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "MixITConfig":
        path = path or Path.home() / MixITConstants.CONFIG_DIR / MixITConstants.CONFIG_FILE
        if not path.exists():
            return cls()
        with open(path) as f:
            return cls.from_dict(json.load(f))


# -----------------------------------------------------------------------------
# Data models: Stem, Bid, Collab
# ------------------------------------------------------------------------------

class StemStatus(Enum):
    UNKNOWN = 0
    FILLED = 1
    DELISTED = 2
    EXPIRED = 3
    ACTIVE = 4


class BidStatus(Enum):
    UNKNOWN = 0
    FILLED = 1
    CANCELLED = 2
    EXPIRED = 3
    ACTIVE = 4


@dataclass
class StemListing:
    stem_id: str
    lister: str
    content_hash: str
    ask_wei: int
    listed_at_block: int
    expiry_block: int
    filled: bool
    delisted: bool
    volume_wei: int = 0
    royalty_paid: int = 0
    bid_count: int = 0
    collab_count: int = 0

    @property
    def is_active(self) -> bool:
        return not self.filled and not self.delisted and self.expiry_block > 0

    def to_display(self, block: int = 0) -> str:
        status = "active" if self.is_active and (block == 0 or block < self.expiry_block) else "inactive"
        return (
            f"Stem {self.stem_id[:16]}... | lister={self.lister[:10]}... | "
            f"ask={self.ask_wei} wei | {status}"
        )


@dataclass
class BidRecord:
    bid_id: str
    stem_id: str
    bidder: str
    bid_wei: int
    placed_at_block: int
    expiry_block: int
    filled: bool
    cancelled: bool
    stem_lister: str = ""
    stem_ask_wei: int = 0

    @property
    def is_active(self) -> bool:
        return not self.filled and not self.cancelled and self.expiry_block > 0

    def to_display(self, block: int = 0) -> str:
        status = "active" if self.is_active and (block == 0 or block < self.expiry_block) else "inactive"
        return (
            f"Bid {self.bid_id[:16]}... | stem={self.stem_id[:16]}... | "
            f"bidder={self.bidder[:10]}... | {self.bid_wei} wei | {status}"
        )


@dataclass
class CollabInvite:
    collab_id: str
    stem_id: str
    inviter: str
    invitee: str
    share_bps: int
    sent_at_block: int
    accepted: bool
    rejected: bool

    def to_display(self) -> str:
        state = "accepted" if self.accepted else ("rejected" if self.rejected else "pending")
        return (
            f"Collab {self.collab_id[:16]}... | stem={self.stem_id[:16]}... | "
            f"inviter={self.inviter[:10]}... -> invitee={self.invitee[:10]}... | "
            f"share={self.share_bps} bps | {state}"
        )


@dataclass
class ExchangeStats:
    total_stems_listed: int
    total_bids_placed: int
    total_volume: int
    total_fees: int
    treasury_accum: int
    vault_accum: int
    current_block: int = 0

    def to_display(self) -> str:
        return (
            f"Stems: {self.total_stems_listed} | Bids: {self.total_bids_placed} | "
            f"Volume: {self.total_volume} wei | Fees: {self.total_fees} wei | "
            f"Treasury accum: {self.treasury_accum} | Vault accum: {self.vault_accum}"
        )


# -----------------------------------------------------------------------------
# Content hash and stem helpers (music / remix)
# ------------------------------------------------------------------------------

def content_hash_from_bytes(data: bytes) -> str:
    h = hashlib.sha256(data).hexdigest()
    return "0x" + h.zfill(64)[:64]


def content_hash_from_string(s: str) -> str:
    return content_hash_from_bytes(s.encode("utf-8"))


def content_hash_from_file(path: Union[str, Path]) -> str:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(str(path))
    with open(path, "rb") as f:
        return content_hash_from_bytes(f.read())


def random_content_hash() -> str:
    return "0x" + secrets.token_hex(32)


def stem_id_compute(content_hash: str, lister: str, seq: int) -> str:
    from hashlib import sha256
    payload = f"{content_hash}_{lister}_{seq}"
    return "0x" + sha256(payload.encode()).hexdigest()


def bid_id_compute(stem_id: str, bidder: str, bid_wei: int, seq: int) -> str:
    from hashlib import sha256
    payload = f"{stem_id}_{bidder}_{bid_wei}_{seq}"
    return "0x" + sha256(payload.encode()).hexdigest()


def collab_id_compute(stem_id: str, inviter: str, invitee: str, seq: int) -> str:
    from hashlib import sha256
    payload = f"{stem_id}_{inviter}_{invitee}_{seq}"
    return "0x" + sha256(payload.encode()).hexdigest()


# -----------------------------------------------------------------------------
# Wei / ETH formatting
# ------------------------------------------------------------------------------

def wei_to_eth(wei: int) -> float:
    return wei / 1e18


def eth_to_wei(eth: float) -> int:
    return int(eth * 1e18)


def format_wei(wei: int) -> str:
    return f"{wei_to_eth(wei):.6f} ETH"


def parse_wei(s: str) -> int:
    s = s.strip().upper().replace(",", "")
    if s.endswith("ETH"):
        return eth_to_wei(float(s[:-3].strip()))
    if s.endswith("WEI"):
        return int(s[:-3].strip())
    return int(s)


# -----------------------------------------------------------------------------
# Contract ABI (minimal for MixFinex-style)
# ------------------------------------------------------------------------------

MIXFINEX_ABI = [
    {"inputs": [], "name": "getConfig", "outputs": [{"internalType": "address", "name": "_treasury", "type": "address"}, {"internalType": "address", "name": "_feeVault", "type": "address"}, {"internalType": "address", "name": "_exchangeKeeper", "type": "address"}, {"internalType": "address", "name": "_keeper", "type": "address"}, {"internalType": "uint256", "name": "_feeBps", "type": "uint256"}, {"internalType": "uint256", "name": "_minListingWei", "type": "uint256"}, {"internalType": "uint256", "name": "_maxListingWei", "type": "uint256"}, {"internalType": "uint256", "name": "_defaultExpiryBlocks", "type": "uint256"}, {"internalType": "uint256", "name": "_deployedBlock", "type": "uint256"}, {"internalType": "bool", "name": "_exchangePaused", "type": "bool"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"internalType": "bytes32", "name": "stemId", "type": "bytes32"}], "name": "getStem", "outputs": [{"internalType": "address", "name": "lister", "type": "address"}, {"internalType": "bytes32", "name": "contentHash", "type": "bytes32"}, {"internalType": "uint256", "name": "askWei", "type": "uint256"}, {"internalType": "uint256", "name": "listedAtBlock", "type": "uint256"}, {"internalType": "uint256", "name": "expiryBlock", "type": "uint256"}, {"internalType": "bool", "name": "filled", "type": "bool"}, {"internalType": "bool", "name": "delisted", "type": "bool"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"internalType": "bytes32", "name": "bidId", "type": "bytes32"}], "name": "getBid", "outputs": [{"internalType": "bytes32", "name": "stemId", "type": "bytes32"}, {"internalType": "address", "name": "bidder", "type": "address"}, {"internalType": "uint256", "name": "bidWei", "type": "uint256"}, {"internalType": "uint256", "name": "placedAtBlock", "type": "uint256"}, {"internalType": "uint256", "name": "expiryBlock", "type": "uint256"}, {"internalType": "bool", "name": "filled", "type": "bool"}, {"internalType": "bool", "name": "cancelled", "type": "bool"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"internalType": "bytes32", "name": "contentHash", "type": "bytes32"}, {"internalType": "uint256", "name": "askWei", "type": "uint256"}], "name": "listStem", "outputs": [{"internalType": "bytes32", "name": "stemId", "type": "bytes32"}], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"internalType": "bytes32", "name": "stemId", "type": "bytes32"}, {"internalType": "uint256", "name": "bidWei", "type": "uint256"}], "name": "placeBid", "outputs": [{"internalType": "bytes32", "name": "bidId", "type": "bytes32"}], "stateMutability": "payable", "type": "function"},
    {"inputs": [{"internalType": "bytes32", "name": "stemId", "type": "bytes32"}], "name": "fillStemOffer", "outputs": [], "stateMutability": "payable", "type": "function"},
    {"inputs": [{"internalType": "bytes32", "name": "bidId", "type": "bytes32"}], "name": "fillBid", "outputs": [], "stateMutability": "payable", "type": "function"},
    {"inputs": [{"internalType": "bytes32", "name": "stemId", "type": "bytes32"}], "name": "delistStem", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"internalType": "bytes32", "name": "bidId", "type": "bytes32"}], "name": "cancelBid", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [], "name": "getExchangeStats", "outputs": [{"internalType": "uint256", "name": "totalStemsListed", "type": "uint256"}, {"internalType": "uint256", "name": "totalBidsPlaced", "type": "uint256"}, {"internalType": "uint256", "name": "totalVolume", "type": "uint256"}, {"internalType": "uint256", "name": "totalFees", "type": "uint256"}, {"internalType": "uint256", "name": "treasuryAccum", "type": "uint256"}, {"internalType": "uint256", "name": "vaultAccum", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"internalType": "address", "name": "lister", "type": "address"}], "name": "getStemIdsByLister", "outputs": [{"internalType": "bytes32[]", "name": "", "type": "bytes32[]"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"internalType": "address", "name": "bidder", "type": "address"}], "name": "getBidIdsByBidder", "outputs": [{"internalType": "bytes32[]", "name": "", "type": "bytes32[]"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"internalType": "bytes32", "name": "stemId", "type": "bytes32"}], "name": "canFillStem", "outputs": [{"internalType": "bool", "name": "", "type": "bool"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"internalType": "bytes32", "name": "bidId", "type": "bytes32"}], "name": "canFillBid", "outputs": [{"internalType": "bool", "name": "", "type": "bool"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"internalType": "bytes32", "name": "contentHash", "type": "bytes32"}, {"internalType": "address", "name": "lister", "type": "address"}, {"internalType": "uint256", "name": "seq", "type": "uint256"}], "name": "computeStemId", "outputs": [{"internalType": "bytes32", "name": "", "type": "bytes32"}], "stateMutability": "pure", "type": "function"},
    {"inputs": [{"internalType": "bytes32", "name": "stemId", "type": "bytes32"}, {"internalType": "address", "name": "bidder", "type": "address"}, {"internalType": "uint256", "name": "bidWei", "type": "uint256"}, {"internalType": "uint256", "name": "seq", "type": "uint256"}], "name": "computeBidId", "outputs": [{"internalType": "bytes32", "name": "", "type": "bytes32"}], "stateMutability": "pure", "type": "function"},
]


# -----------------------------------------------------------------------------
# Hex / bytes32 conversion
# ------------------------------------------------------------------------------

def hex_to_bytes32(s: str) -> bytes:
    s = s.replace("0x", "").lower()
    if len(s) != 64:
        s = s.zfill(64)[:64]
    return bytes.fromhex(s)


def bytes32_to_hex(b: bytes) -> str:
    return "0x" + b.hex().zfill(64)[:64]


def address_to_hex(addr: str) -> str:
    addr = addr.replace("0x", "").lower()
    return "0x" + addr.zfill(40)[:40]


# -----------------------------------------------------------------------------
# RPC client (no web3 dependency for minimal setup)
# ------------------------------------------------------------------------------

def _rpc_call(url: str, method: str, params: List[Any]) -> Any:
    import urllib.request
    body = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 1}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        out = json.loads(r.read().decode())
    if "error" in out:
        raise RuntimeError(out["error"].get("message", str(out["error"])))
    return out.get("result")


def rpc_eth_block_number(url: str) -> int:
    return int(_rpc_call(url, "eth_blockNumber", []), 16)


def rpc_eth_call(url: str, to: str, data: str) -> str:
    return _rpc_call(url, "eth_call", [{"to": to, "data": data}, "latest"])


def rpc_eth_chain_id(url: str) -> int:
    return int(_rpc_call(url, "eth_chainId", []), 16)


# -----------------------------------------------------------------------------
# ABI encoding (minimal: function selector + uint256, address, bytes32)
# ------------------------------------------------------------------------------

def _abi_selector(signature: str) -> bytes:
    h = _keccak256(signature.encode()).hex()
    return bytes.fromhex(h[:8])


def _encode_uint256(v: int) -> bytes:
    return v.to_bytes(32, "big")


def _encode_address(addr: str) -> bytes:
    addr = addr.replace("0x", "").lower().zfill(40)[-40:]
    return bytes.fromhex(addr).rjust(32, b"\x00")


def _encode_bytes32(s: str) -> bytes:
    b = hex_to_bytes32(s)
    return b.rjust(32, b"\x00") if len(b) < 32 else b[:32]


def _decode_uint256(data: bytes) -> int:
    return int.from_bytes(data[-32:], "big")


def _decode_address(data: bytes) -> str:
    return "0x" + data[-20:].hex()


def _decode_bytes32(data: bytes) -> str:
    return "0x" + data[-32:].hex()


# -----------------------------------------------------------------------------
# MixFinex contract client (read-only via RPC)
# ------------------------------------------------------------------------------

class MixFinexClient:
    def __init__(self, rpc_url: str, contract_address: str):
        self.rpc_url = rpc_url
        self.contract = address_to_hex(contract_address)
        self._selector_cache: Dict[str, bytes] = {}

    def _call(self, sig: str, *args_encoded: bytes) -> str:
        sel = self._selector_cache.get(sig)
        if sel is None:
            sel = _abi_selector(sig)
            self._selector_cache[sig] = sel
        data = "0x" + (sel + b"".join(args_encoded)).hex()
        return rpc_eth_call(self.rpc_url, self.contract, data)

    def get_config(self) -> Dict[str, Any]:
        data = self._call("getConfig()")
        if not data or data == "0x":
            return {}
        raw = bytes.fromhex(data.replace("0x", ""))
        return {
            "treasury": _decode_address(raw[0:32]),
            "fee_vault": _decode_address(raw[32:64]),
            "exchange_keeper": _decode_address(raw[64:96]),
            "keeper": _decode_address(raw[96:128]),
            "fee_bps": _decode_uint256(raw[128:160]),
            "min_listing_wei": _decode_uint256(raw[160:192]),
            "max_listing_wei": _decode_uint256(raw[192:224]),
            "default_expiry_blocks": _decode_uint256(raw[224:256]),
            "deployed_block": _decode_uint256(raw[256:288]),
            "exchange_paused": _decode_uint256(raw[288:320]) != 0,
        }

    def get_stem(self, stem_id: str) -> Optional[StemListing]:
        try:
            data = self._call("getStem(bytes32)", _encode_bytes32(stem_id))
            if not data or data == "0x" or len(data) < 2 + 32 * 7 * 2:
                return None
            raw = bytes.fromhex(data.replace("0x", ""))
            offset = 0
            lister = _decode_address(raw[offset:offset+32]); offset += 32
            content_hash = _decode_bytes32(raw[offset:offset+32]); offset += 32
            ask_wei = _decode_uint256(raw[offset:offset+32]); offset += 32
            listed_at = _decode_uint256(raw[offset:offset+32]); offset += 32
            expiry = _decode_uint256(raw[offset:offset+32]); offset += 32
            filled = _decode_uint256(raw[offset:offset+32]) != 0; offset += 32
            delisted = _decode_uint256(raw[offset:offset+32]) != 0
            return StemListing(
                stem_id=stem_id,
                lister=lister,
                content_hash=content_hash,
                ask_wei=ask_wei,
                listed_at_block=listed_at,
                expiry_block=expiry,
                filled=filled,
                delisted=delisted,
            )
        except Exception:
            return None

    def get_bid(self, bid_id: str) -> Optional[BidRecord]:
        try:
            data = self._call("getBid(bytes32)", _encode_bytes32(bid_id))
            if not data or data == "0x" or len(data) < 2 + 32 * 7 * 2:
                return None
            raw = bytes.fromhex(data.replace("0x", ""))
            offset = 0
            stem_id = _decode_bytes32(raw[offset:offset+32]); offset += 32
            bidder = _decode_address(raw[offset:offset+32]); offset += 32
            bid_wei = _decode_uint256(raw[offset:offset+32]); offset += 32
            placed_at = _decode_uint256(raw[offset:offset+32]); offset += 32
            expiry = _decode_uint256(raw[offset:offset+32]); offset += 32
            filled = _decode_uint256(raw[offset:offset+32]) != 0; offset += 32
            cancelled = _decode_uint256(raw[offset:offset+32]) != 0
            return BidRecord(
                bid_id=bid_id,
                stem_id=stem_id,
                bidder=bidder,
                bid_wei=bid_wei,
                placed_at_block=placed_at,
                expiry_block=expiry,
                filled=filled,
                cancelled=cancelled,
            )
        except Exception:
            return None

    def get_exchange_stats(self) -> ExchangeStats:
        try:
            data = self._call("getExchangeStats()")
            if not data or data == "0x":
                return ExchangeStats(0, 0, 0, 0, 0, 0)
            raw = bytes.fromhex(data.replace("0x", ""))
            return ExchangeStats(
                total_stems_listed=_decode_uint256(raw[0:32]),
                total_bids_placed=_decode_uint256(raw[32:64]),
                total_volume=_decode_uint256(raw[64:96]),
                total_fees=_decode_uint256(raw[96:128]),
                treasury_accum=_decode_uint256(raw[128:160]),
                vault_accum=_decode_uint256(raw[160:192]),
                current_block=rpc_eth_block_number(self.rpc_url),
            )
        except Exception:
            return ExchangeStats(0, 0, 0, 0, 0, 0)

    def get_stem_ids_by_lister(self, lister: str) -> List[str]:
        try:
            data = self._call("getStemIdsByLister(address)", _encode_address(lister))
            if not data or data == "0x":
                return []
            raw = bytes.fromhex(data.replace("0x", ""))
            n = _decode_uint256(raw[0:32])
            out = []
            for i in range(n):
                out.append(_decode_bytes32(raw[32 + i * 32:32 + (i + 1) * 32]))
            return out
        except Exception:
            return []

    def get_bid_ids_by_bidder(self, bidder: str) -> List[str]:
        try:
            data = self._call("getBidIdsByBidder(address)", _encode_address(bidder))
            if not data or data == "0x":
                return []
            raw = bytes.fromhex(data.replace("0x", ""))
            n = _decode_uint256(raw[0:32])
            out = []
            for i in range(n):
                out.append(_decode_bytes32(raw[32 + i * 32:32 + (i + 1) * 32]))
            return out
        except Exception:
            return []

    def can_fill_stem(self, stem_id: str) -> bool:
        try:
            data = self._call("canFillStem(bytes32)", _encode_bytes32(stem_id))
            if not data or data == "0x":
                return False
            return _decode_uint256(bytes.fromhex(data.replace("0x", ""))) != 0
        except Exception:
            return False

    def can_fill_bid(self, bid_id: str) -> bool:
        try:
            data = self._call("canFillBid(bytes32)", _encode_bytes32(bid_id))
            if not data or data == "0x":
                return False
            return _decode_uint256(bytes.fromhex(data.replace("0x", ""))) != 0
        except Exception:
            return False

    def current_block(self) -> int:
        return rpc_eth_block_number(self.rpc_url)


# -----------------------------------------------------------------------------
# Catalog (local mock for stems / remixes)
# ------------------------------------------------------------------------------

@dataclass
class CatalogEntry:
    name: str
    content_hash: str
    artist: str
    genre: str
    duration_sec: int
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "content_hash": self.content_hash,
            "artist": self.artist,
            "genre": self.genre,
            "duration_sec": self.duration_sec,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CatalogEntry":
        return cls(
            name=d.get("name", ""),
            content_hash=d.get("content_hash", ""),
            artist=d.get("artist", ""),
            genre=d.get("genre", ""),
            duration_sec=int(d.get("duration_sec", 0)),
            created_at=float(d.get("created_at", time.time())),
        )


class MixITCatalog:
    def __init__(self, path: Optional[Path] = None):
        self.path = path or Path.home() / MixITConstants.CONFIG_DIR / "catalog.json"
        self.entries: Dict[str, CatalogEntry] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
