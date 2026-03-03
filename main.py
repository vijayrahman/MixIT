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
                with open(self.path) as f:
                    data = json.load(f)
                for k, v in data.get("entries", {}).items():
                    self.entries[k] = CatalogEntry.from_dict(v)
            except Exception:
                pass

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w") as f:
            json.dump({"entries": {k: v.to_dict() for k, v in self.entries.items()}}, f, indent=2)

    def add(self, entry: CatalogEntry) -> None:
        self.entries[entry.content_hash] = entry
        self.save()

    def get(self, content_hash: str) -> Optional[CatalogEntry]:
        return self.entries.get(content_hash)

    def list_all(self) -> List[CatalogEntry]:
        return list(self.entries.values())

    def remove(self, content_hash: str) -> bool:
        if content_hash in self.entries:
            del self.entries[content_hash]
            self.save()
            return True
        return False


# -----------------------------------------------------------------------------
# Collaboration and royalty helpers
# ------------------------------------------------------------------------------

def compute_royalty_split(amount_wei: int, share_bps: int) -> int:
    return (amount_wei * share_bps) // MixITConstants.BPS_DENOM


def compute_fee(amount_wei: int, fee_bps: int) -> int:
    return (amount_wei * fee_bps) // MixITConstants.BPS_DENOM


def compute_net_after_fee(amount_wei: int, fee_bps: int) -> int:
    return amount_wei - compute_fee(amount_wei, fee_bps)


def split_royalty_among(amount_wei: int, shares_bps: List[int]) -> List[int]:
    total_bps = sum(shares_bps)
    if total_bps == 0:
        return [0] * len(shares_bps)
    return [(amount_wei * bps) // total_bps for bps in shares_bps]


# -----------------------------------------------------------------------------
# CLI commands
# ------------------------------------------------------------------------------

def cmd_config(args: List[str], config: MixITConfig) -> None:
    if not args:
        print(json.dumps(config.to_dict(), indent=2))
        return
    if args[0] == "set":
        if len(args) < 3:
            print("Usage: config set <key> <value>")
            return
        key, val = args[1], args[2]
        if key == "rpc_url":
            config.rpc_url = val
        elif key == "chain_id":
            config.chain_id = int(val)
        elif key == "contract_address":
            config.contract_address = val
        elif key == "treasury":
            config.treasury = val
        elif key == "fee_vault":
            config.fee_vault = val
        config.save()
        print(f"Set {key} = {val}")
    elif args[0] == "addresses":
        for i in range(5):
            print(random_address_eip55())


def cmd_catalog(args: List[str], catalog: MixITCatalog) -> None:
    if not args:
        for e in catalog.list_all():
            print(f"  {e.content_hash} | {e.name} | {e.artist} | {e.genre}")
        return
    if args[0] == "add":
        name = args[1] if len(args) > 1 else "Untitled"
        ch = random_content_hash()
        entry = CatalogEntry(name=name, content_hash=ch, artist="", genre="", duration_sec=0)
        catalog.add(entry)
        print(f"Added: {ch} | {name}")
    elif args[0] == "hash":
        if len(args) < 2:
            print("Usage: catalog hash <string_or_file>")
            return
        path = Path(args[1])
        if path.exists():
            print(content_hash_from_file(path))
        else:
            print(content_hash_from_string(args[1]))


def cmd_stats(args: List[str], client: Optional[MixFinexClient]) -> None:
    if not client:
        print("No contract configured. Set contract_address in config.")
        return
    s = client.get_exchange_stats()
    s.current_block = client.current_block()
    print(s.to_display())
    print(f"Current block: {s.current_block}")


def cmd_stem(args: List[str], client: Optional[MixFinexClient]) -> None:
    if not client:
        print("No contract configured.")
        return
    if len(args) < 2:
        print("Usage: stem get <stemId>")
        return
    stem_id = args[1]
    if args[0] == "get":
        s = client.get_stem(stem_id)
        if s:
            print(s.to_display(client.current_block()))
        else:
            print("Stem not found or invalid id.")


def cmd_bid(args: List[str], client: Optional[MixFinexClient]) -> None:
    if not client:
        print("No contract configured.")
        return
    if len(args) < 2:
        print("Usage: bid get <bidId>")
        return
    bid_id = args[1]
    if args[0] == "get":
        b = client.get_bid(bid_id)
        if b:
            print(b.to_display(client.current_block()))
        else:
            print("Bid not found or invalid id.")


def cmd_lister(args: List[str], client: Optional[MixFinexClient]) -> None:
    if not client:
        print("No contract configured.")
        return
    if len(args) < 2:
        print("Usage: lister <address>")
        return
    addr = address_to_hex(args[1])
    ids = client.get_stem_ids_by_lister(addr)
    print(f"Stem ids for {addr}: {len(ids)}")
    for i in ids[:20]:
        print(f"  {i}")
    if len(ids) > 20:
        print(f"  ... and {len(ids) - 20} more")


def cmd_bidder(args: List[str], client: Optional[MixFinexClient]) -> None:
    if not client:
        print("No contract configured.")
        return
    if len(args) < 2:
        print("Usage: bidder <address>")
        return
    addr = address_to_hex(args[1])
    ids = client.get_bid_ids_by_bidder(addr)
    print(f"Bid ids for {addr}: {len(ids)}")
    for i in ids[:20]:
        print(f"  {i}")
    if len(ids) > 20:
        print(f"  ... and {len(ids) - 20} more")


def cmd_can_fill(args: List[str], client: Optional[MixFinexClient]) -> None:
    if not client:
        print("No contract configured.")
        return
    if len(args) < 3:
        print("Usage: canfill stem <stemId> | canfill bid <bidId>")
        return
    kind, id_ = args[1], args[2]
    if kind == "stem":
        print(client.can_fill_stem(id_))
    else:
        print(client.can_fill_bid(id_))


def cmd_fee(args: List[str]) -> None:
    if len(args) < 2:
        print("Usage: fee <amountWei> [feeBps]")
        return
    amount = parse_wei(args[1])
    fee_bps = int(args[2]) if len(args) > 2 else 35
    f = compute_fee(amount, fee_bps)
    net = amount - f
    print(f"Amount: {amount} wei | Fee ({fee_bps} bps): {f} wei | Net: {net} wei")


def cmd_royalty(args: List[str]) -> None:
    if len(args) < 3:
        print("Usage: royalty <amountWei> <shareBps>")
        return
    amount = parse_wei(args[1])
    share_bps = int(args[2])
    r = compute_royalty_split(amount, share_bps)
    print(f"Amount: {amount} wei | Share {share_bps} bps => {r} wei")


def cmd_help() -> None:
    print("MixIT — Music sharing and trading (MixFinex-style)")
    print("  config [set <key> <value> | addresses]")
    print("  catalog [add [name] | hash <string_or_file>]")
    print("  stats")
    print("  stem get <stemId>")
    print("  bid get <bidId>")
    print("  lister <address>")
    print("  bidder <address>")
    print("  canfill stem <stemId> | canfill bid <bidId>")
    print("  fee <amountWei> [feeBps]")
    print("  royalty <amountWei> <shareBps>")
    print("  export catalog | export remixes")
    print("  report [lister <addr> | bidder <addr>]")
    print("  remix [add [title] [parentStemId]]")
    print("  build list|bid|fillstem|fillbid|delist|cancel ...")
    print("  genaddresses [count]")
    print("  volume [stem <stemId> | lister <address>]")
    print("  limits")
    print("  paused")
    print("  liststems <listerAddress> [limit]")
    print("  listbids <bidderAddress> [limit]")
    print("  collabshares <weight1> <weight2> [...]")
    print("  validate")
    print("  block")
    print("  chain")
    print("  health")
    print("  expiry <blocks> | expiry days <number>")
    print("  demo catalog [n] | demo remixes [n]")
    print("  checksum <address>")
    print("  about")
    print("  info")
    print("  version")


def cmd_version() -> None:
    print(f"{MixITConstants.APP_NAME} {MixITConstants.VERSION}")


# -----------------------------------------------------------------------------
# Remix and stem metadata (extended)
# ------------------------------------------------------------------------------

@dataclass
class RemixMetadata:
    title: str
    content_hash: str
    parent_stem_id: str
    creator: str
    bpm: int
    key: str
    tags: List[str]
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "content_hash": self.content_hash,
            "parent_stem_id": self.parent_stem_id,
            "creator": self.creator,
            "bpm": self.bpm,
            "key": self.key,
            "tags": self.tags,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "RemixMetadata":
        return cls(
            title=d.get("title", ""),
            content_hash=d.get("content_hash", ""),
            parent_stem_id=d.get("parent_stem_id", ""),
            creator=d.get("creator", ""),
            bpm=int(d.get("bpm", 0)),
            key=d.get("key", ""),
            tags=list(d.get("tags", [])),
            created_at=float(d.get("created_at", time.time())),
        )


class RemixRegistry:
    def __init__(self, path: Optional[Path] = None):
        self.path = path or Path.home() / MixITConstants.CONFIG_DIR / "remixes.json"
        self.remixes: Dict[str, RemixMetadata] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                with open(self.path) as f:
                    data = json.load(f)
                for k, v in data.get("remixes", {}).items():
                    self.remixes[k] = RemixMetadata.from_dict(v)
            except Exception:
                pass

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w") as f:
            json.dump({"remixes": {k: v.to_dict() for k, v in self.remixes.items()}}, f, indent=2)

    def add(self, meta: RemixMetadata) -> None:
        self.remixes[meta.content_hash] = meta
        self.save()

    def get(self, content_hash: str) -> Optional[RemixMetadata]:
        return self.remixes.get(content_hash)

    def by_parent(self, parent_stem_id: str) -> List[RemixMetadata]:
        return [r for r in self.remixes.values() if r.parent_stem_id == parent_stem_id]

    def list_all(self) -> List[RemixMetadata]:
        return list(self.remixes.values())


# -----------------------------------------------------------------------------
# Export / report generation
# ------------------------------------------------------------------------------

def export_catalog_json(catalog: MixITCatalog) -> str:
    return json.dumps(
        {"entries": [e.to_dict() for e in catalog.list_all()], "version": 1},
        indent=2,
    )


def export_remixes_json(registry: RemixRegistry) -> str:
    return json.dumps(
        {"remixes": [r.to_dict() for r in registry.list_all()], "version": 1},
        indent=2,
    )


def report_exchange_stats(client: MixFinexClient) -> Dict[str, Any]:
    s = client.get_exchange_stats()
    s.current_block = client.current_block()
    return {
        "total_stems_listed": s.total_stems_listed,
        "total_bids_placed": s.total_bids_placed,
        "total_volume_wei": s.total_volume,
        "total_fees_wei": s.total_fees,
        "treasury_accum_wei": s.treasury_accum,
        "vault_accum_wei": s.vault_accum,
        "current_block": s.current_block,
    }


def report_lister_activity(client: MixFinexClient, lister: str) -> Dict[str, Any]:
    addr = address_to_hex(lister)
    stem_ids = client.get_stem_ids_by_lister(addr)
    stems = []
    for sid in stem_ids[:50]:
        s = client.get_stem(sid)
        if s:
            stems.append({
                "stem_id": sid,
                "ask_wei": s.ask_wei,
                "filled": s.filled,
                "delisted": s.delisted,
                "expiry_block": s.expiry_block,
            })
    return {"lister": addr, "stem_count": len(stem_ids), "stems_sample": stems}


def report_bidder_activity(client: MixFinexClient, bidder: str) -> Dict[str, Any]:
    addr = address_to_hex(bidder)
    bid_ids = client.get_bid_ids_by_bidder(addr)
    bids = []
    for bid in bid_ids[:50]:
        b = client.get_bid(bid)
        if b:
            bids.append({
                "bid_id": bid,
                "stem_id": b.stem_id,
                "bid_wei": b.bid_wei,
                "filled": b.filled,
                "cancelled": b.cancelled,
                "expiry_block": b.expiry_block,
            })
    return {"bidder": addr, "bid_count": len(bid_ids), "bids_sample": bids}


# -----------------------------------------------------------------------------
# Wallet simulation (no private key sign; build intent only)
# ------------------------------------------------------------------------------

@dataclass
class TxIntent:
    to: str
    value_wei: int
    data_hex: str
    gas_limit: int
    description: str


def build_list_stem_intent(contract: str, content_hash_hex: str, ask_wei: int) -> TxIntent:
    sel = _abi_selector("listStem(bytes32,uint256)")
    data = sel + _encode_bytes32(content_hash_hex) + _encode_uint256(ask_wei)
    return TxIntent(
        to=contract,
        value_wei=0,
        data_hex="0x" + data.hex(),
        gas_limit=200_000,
        description="listStem",
    )


def build_place_bid_intent(contract: str, stem_id_hex: str, bid_wei: int) -> TxIntent:
    sel = _abi_selector("placeBid(bytes32,uint256)")
    data = sel + _encode_bytes32(stem_id_hex) + _encode_uint256(bid_wei)
    return TxIntent(
        to=contract,
        value_wei=bid_wei,
        data_hex="0x" + data.hex(),
        gas_limit=250_000,
        description="placeBid",
    )


def build_fill_stem_intent(contract: str, stem_id_hex: str, ask_wei: int) -> TxIntent:
    sel = _abi_selector("fillStemOffer(bytes32)")
    data = sel + _encode_bytes32(stem_id_hex)
    return TxIntent(
        to=contract,
        value_wei=ask_wei,
        data_hex="0x" + data.hex(),
        gas_limit=150_000,
        description="fillStemOffer",
    )


def build_fill_bid_intent(contract: str, bid_id_hex: str) -> TxIntent:
    sel = _abi_selector("fillBid(bytes32)")
    data = sel + _encode_bytes32(bid_id_hex)
    return TxIntent(
        to=contract,
        value_wei=0,
        data_hex="0x" + data.hex(),
        gas_limit=150_000,
        description="fillBid",
    )


def build_delist_stem_intent(contract: str, stem_id_hex: str) -> TxIntent:
    sel = _abi_selector("delistStem(bytes32)")
    data = sel + _encode_bytes32(stem_id_hex)
    return TxIntent(
        to=contract,
        value_wei=0,
        data_hex="0x" + data.hex(),
        gas_limit=100_000,
        description="delistStem",
    )


def build_cancel_bid_intent(contract: str, bid_id_hex: str) -> TxIntent:
    sel = _abi_selector("cancelBid(bytes32)")
    data = sel + _encode_bytes32(bid_id_hex)
    return TxIntent(
        to=contract,
        value_wei=0,
        data_hex="0x" + data.hex(),
        gas_limit=100_000,
        description="cancelBid",
    )


# -----------------------------------------------------------------------------
# Additional CLI: export, report, remix, build
# ------------------------------------------------------------------------------

def cmd_export(args: List[str], catalog: MixITCatalog, registry: RemixRegistry) -> None:
    if not args:
        print("Usage: export catalog | export remixes")
        return
    if args[0] == "catalog":
        print(export_catalog_json(catalog))
    elif args[0] == "remixes":
        print(export_remixes_json(registry))


def cmd_report(args: List[str], client: Optional[MixFinexClient]) -> None:
    if not client:
        print("No contract configured.")
        return
    if not args:
        print(json.dumps(report_exchange_stats(client), indent=2))
        return
    if args[0] == "lister" and len(args) > 1:
        print(json.dumps(report_lister_activity(client, args[1]), indent=2))
    elif args[0] == "bidder" and len(args) > 1:
        print(json.dumps(report_bidder_activity(client, args[1]), indent=2))
    else:
        print("Usage: report | report lister <addr> | report bidder <addr>")


def cmd_remix(args: List[str], registry: RemixRegistry) -> None:
    if not args:
        for r in registry.list_all():
            print(f"  {r.content_hash} | {r.title} | parent={r.parent_stem_id[:16]}...")
        return
    if args[0] == "add":
        title = args[1] if len(args) > 1 else "Untitled Remix"
        parent = args[2] if len(args) > 2 else "0x" + "0" * 64
        ch = random_content_hash()
        meta = RemixMetadata(
            title=title,
            content_hash=ch,
            parent_stem_id=parent,
            creator="",
            bpm=0,
            key="",
            tags=[],
        )
        registry.add(meta)
        print(f"Added remix: {ch} | {title}")


def cmd_build(args: List[str], config: MixITConfig) -> None:
    if not config.contract_address or len(args) < 2:
        print("Usage: build list <contentHash> <askWei> | build bid <stemId> <bidWei> | build fillstem <stemId> <askWei> | build fillbid <bidId> | build delist <stemId> | build cancel <bidId>")
        return
    contract = address_to_hex(config.contract_address)
    kind = args[0].lower()
    if kind == "list" and len(args) >= 4:
        intent = build_list_stem_intent(contract, args[1], parse_wei(args[2]))
    elif kind == "bid" and len(args) >= 4:
        intent = build_place_bid_intent(contract, args[1], parse_wei(args[2]))
    elif kind == "fillstem" and len(args) >= 4:
        intent = build_fill_stem_intent(contract, args[1], parse_wei(args[2]))
    elif kind == "fillbid" and len(args) >= 3:
        intent = build_fill_bid_intent(contract, args[1])
    elif kind == "delist" and len(args) >= 2:
        intent = build_delist_stem_intent(contract, args[1])
    elif kind == "cancel" and len(args) >= 2:
        intent = build_cancel_bid_intent(contract, args[1])
    else:
        print("Unknown or incomplete build command.")
        return
    print(json.dumps({
        "to": intent.to,
        "value_wei": intent.value_wei,
        "data": intent.data_hex,
        "gas_limit": intent.gas_limit,
        "description": intent.description,
    }, indent=2))


# -----------------------------------------------------------------------------
# EIP-55 batch generator (for reference)
# ------------------------------------------------------------------------------

def generate_eip55_addresses(count: int = 10) -> List[str]:
    return [random_address_eip55() for _ in range(count)]


def cmd_gen_addresses(args: List[str]) -> None:
    n = int(args[0]) if args else 10
    for a in generate_eip55_addresses(n):
        print(a)


# -----------------------------------------------------------------------------
# More formatting and validation
# ------------------------------------------------------------------------------

def validate_content_hash(s: str) -> bool:
    s = s.replace("0x", "").lower()
    if len(s) != 64:
        return False
    return all(c in "0123456789abcdef" for c in s)


def validate_address(s: str) -> bool:
    s = s.replace("0x", "").lower()
    if len(s) != 40:
        return False
    return all(c in "0123456789abcdef" for c in s)


def truncate_hex(s: str, head: int = 10, tail: int = 8) -> str:
    if len(s) <= head + tail + 2:
        return s
    return s[: 2 + head] + "..." + s[-tail:]


def format_stem_table(stems: List[StemListing], block: int = 0) -> str:
    lines = ["stem_id | lister | ask_wei | status"]
    for s in stems:
        status = "active" if (not s.filled and not s.delisted and (block == 0 or block < s.expiry_block)) else "inactive"
        lines.append(f"{truncate_hex(s.stem_id)} | {truncate_hex(s.lister)} | {s.ask_wei} | {status}")
    return "\n".join(lines)


def format_bid_table(bids: List[BidRecord], block: int = 0) -> str:
    lines = ["bid_id | stem_id | bidder | bid_wei | status"]
    for b in bids:
        status = "active" if (not b.filled and not b.cancelled and (block == 0 or block < b.expiry_block)) else "inactive"
        lines.append(f"{truncate_hex(b.bid_id)} | {truncate_hex(b.stem_id)} | {truncate_hex(b.bidder)} | {b.bid_wei} | {status}")
    return "\n".join(lines)


# -----------------------------------------------------------------------------
# Constants for display and defaults
# ------------------------------------------------------------------------------

DISPLAY_DECIMALS_ETH = 6
DISPLAY_TRUNCATE_ADDR = 12
DISPLAY_TRUNCATE_HASH = 16
DEFAULT_FEE_BPS = 35
DEFAULT_EXPIRY_BLOCKS = 50000
MIN_LISTING_WEI_DEFAULT = 1000000000000000
MAX_LISTING_WEI_DEFAULT = 500000000000000000000


def format_eth_short(wei: int) -> str:
    return f"{wei_to_eth(wei):.{DISPLAY_DECIMALS_ETH}f}"


def format_address_short(addr: str) -> str:
    return truncate_hex(addr if addr.startswith("0x") else "0x" + addr, DISPLAY_TRUNCATE_ADDR, 6)


# -----------------------------------------------------------------------------
# Extended contract client (more view wrappers)
# ------------------------------------------------------------------------------

def client_get_stem_volume(client: MixFinexClient, stem_id: str) -> int:
    try:
        sel = _abi_selector("stemVolumeWei(bytes32)")
        data = "0x" + (sel + _encode_bytes32(stem_id)).hex()
        out = rpc_eth_call(client.rpc_url, client.contract, data)
        if not out or out == "0x":
            return 0
        return _decode_uint256(bytes.fromhex(out.replace("0x", "")))
    except Exception:
        return 0


def client_get_lister_volume(client: MixFinexClient, lister: str) -> int:
    try:
        sel = _abi_selector("listerVolumeWei(address)")
        data = "0x" + (sel + _encode_address(lister)).hex()
        out = rpc_eth_call(client.rpc_url, client.contract, data)
        if not out or out == "0x":
            return 0
        return _decode_uint256(bytes.fromhex(out.replace("0x", "")))
    except Exception:
        return 0


def client_get_min_listing_wei(client: MixFinexClient) -> int:
    try:
        sel = _abi_selector("minListingWei()")
        data = "0x" + sel.hex()
        out = rpc_eth_call(client.rpc_url, client.contract, data)
        if not out or out == "0x":
            return 0
        return _decode_uint256(bytes.fromhex(out.replace("0x", "")))
    except Exception:
        return 0


def client_get_max_listing_wei(client: MixFinexClient) -> int:
    try:
        sel = _abi_selector("maxListingWei()")
        data = "0x" + sel.hex()
        out = rpc_eth_call(client.rpc_url, client.contract, data)
        if not out or out == "0x":
            return 0
        return _decode_uint256(bytes.fromhex(out.replace("0x", "")))
    except Exception:
        return 0


def client_get_fee_bps(client: MixFinexClient) -> int:
    try:
        sel = _abi_selector("feeBps()")
        data = "0x" + sel.hex()
        out = rpc_eth_call(client.rpc_url, client.contract, data)
        if not out or out == "0x":
            return 0
        return _decode_uint256(bytes.fromhex(out.replace("0x", "")))
    except Exception:
        return 0


def client_is_paused(client: MixFinexClient) -> bool:
    try:
        sel = _abi_selector("exchangePaused()")
        data = "0x" + sel.hex()
        out = rpc_eth_call(client.rpc_url, client.contract, data)
        if not out or out == "0x":
            return False
        return _decode_uint256(bytes.fromhex(out.replace("0x", ""))) != 0
    except Exception:
        return False


def client_get_default_expiry_blocks(client: MixFinexClient) -> int:
    try:
        sel = _abi_selector("defaultExpiryBlocks()")
        data = "0x" + sel.hex()
        out = rpc_eth_call(client.rpc_url, client.contract, data)
        if not out or out == "0x":
            return 0
        return _decode_uint256(bytes.fromhex(out.replace("0x", "")))
    except Exception:
        return 0


# -----------------------------------------------------------------------------
# CLI: volume, pause, limits
# ------------------------------------------------------------------------------

def cmd_volume(args: List[str], client: Optional[MixFinexClient]) -> None:
    if not client:
        print("No contract configured.")
        return
    if not args:
        s = client.get_exchange_stats()
        print(f"Total volume: {s.total_volume} wei ({format_eth_short(s.total_volume)} ETH)")
        return
    if args[0] == "stem" and len(args) > 1:
        v = client_get_stem_volume(client, args[1])
        print(f"Stem {truncate_hex(args[1])} volume: {v} wei")
    elif args[0] == "lister" and len(args) > 1:
        v = client_get_lister_volume(client, args[1])
        print(f"Lister {truncate_hex(args[1])} volume: {v} wei")
    else:
        print("Usage: volume | volume stem <stemId> | volume lister <address>")


def cmd_limits(args: List[str], client: Optional[MixFinexClient]) -> None:
    if not client:
        print("No contract configured.")
        return
    min_wei = client_get_min_listing_wei(client)
    max_wei = client_get_max_listing_wei(client)
    fee_bps = client_get_fee_bps(client)
    expiry = client_get_default_expiry_blocks(client)
    print(f"Min listing: {min_wei} wei | Max listing: {max_wei} wei")
    print(f"Fee: {fee_bps} bps | Default expiry: {expiry} blocks")


def cmd_paused(args: List[str], client: Optional[MixFinexClient]) -> None:
    if not client:
        print("No contract configured.")
        return
    p = client_is_paused(client)
    print("Exchange paused:" if p else "Exchange active.")


# -----------------------------------------------------------------------------
# Batch fetch stems/bids for display
# ------------------------------------------------------------------------------

def fetch_lister_stems(client: MixFinexClient, lister: str, limit: int = 20) -> List[StemListing]:
    ids = client.get_stem_ids_by_lister(address_to_hex(lister))
    out = []
    for sid in ids[:limit]:
        s = client.get_stem(sid)
        if s:
            out.append(s)
    return out


def fetch_bidder_bids(client: MixFinexClient, bidder: str, limit: int = 20) -> List[BidRecord]:
    ids = client.get_bid_ids_by_bidder(address_to_hex(bidder))
    out = []
    for bid in ids[:limit]:
        b = client.get_bid(bid)
        if b:
            out.append(b)
    return out


def cmd_list_stems(args: List[str], client: Optional[MixFinexClient]) -> None:
    if not client:
        print("No contract configured.")
        return
    if len(args) < 1:
        print("Usage: liststems <listerAddress> [limit]")
        return
    limit = int(args[1]) if len(args) > 1 else 20
    stems_list = fetch_lister_stems(client, args[0], limit)
    block = client.current_block()
    print(format_stem_table(stems_list, block))


def cmd_list_bids(args: List[str], client: Optional[MixFinexClient]) -> None:
    if not client:
        print("No contract configured.")
        return
    if len(args) < 1:
        print("Usage: listbids <bidderAddress> [limit]")
        return
    limit = int(args[1]) if len(args) > 1 else 20
    bids_list = fetch_bidder_bids(client, args[0], limit)
    block = client.current_block()
    print(format_bid_table(bids_list, block))


# -----------------------------------------------------------------------------
# Validation and sanitization
# ------------------------------------------------------------------------------

def sanitize_hex(s: str, length: int = 64) -> str:
    s = s.replace("0x", "").lower().strip()
    s = "".join(c for c in s if c in "0123456789abcdef")
    return ("0x" + s.zfill(length)[:length]) if s else ""


def sanitize_address(s: str) -> str:
    return address_to_hex(s)


def require_positive_int(n: int, name: str = "value") -> None:
