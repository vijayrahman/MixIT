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
