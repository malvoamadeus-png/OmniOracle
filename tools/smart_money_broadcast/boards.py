from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class BoardConfig:
    name: str
    source_kind: str
    sport: Optional[str] = None
    tag_id: Optional[int] = None
    related_tags: bool = False
    slug_prefixes: Tuple[str, ...] = ()
    slug_interval_minutes: int = 15
    slug_format: str = "timestamp"
    max_games: int = 30
    market_kind: str = "all"
    gamma_page_limit: int = 100


BOARD_CATALOG: "OrderedDict[str, BoardConfig]" = OrderedDict(
    (
        (
            "NBA",
            BoardConfig(
                name="NBA",
                source_kind="sport",
                sport="nba",
                max_games=50,
                market_kind="moneyline",
                gamma_page_limit=50,
            ),
        ),
        (
            "CLIMATE",
            BoardConfig(name="CLIMATE", source_kind="tag", sport="climate", tag_id=87, max_games=20),
        ),
        (
            "LOL",
            BoardConfig(name="LOL", source_kind="tag", sport="league-of-legends", tag_id=65, max_games=20),
        ),
        (
            "CS2",
            BoardConfig(name="CS2", source_kind="tag", sport="esports", tag_id=100780, related_tags=True, max_games=20),
        ),
        (
            "UCL",
            BoardConfig(name="UCL", source_kind="tag", sport="soccer", tag_id=100977, related_tags=True, max_games=30),
        ),
        (
            "CHAMPIONS LEAGUE",
            BoardConfig(
                name="CHAMPIONS LEAGUE",
                source_kind="tag",
                sport="soccer",
                tag_id=1234,
                related_tags=True,
                max_games=30,
            ),
        ),
        (
            "SOCCER",
            BoardConfig(name="SOCCER", source_kind="tag", sport="soccer", tag_id=100350, related_tags=True, max_games=30),
        ),
        (
            "15M",
            BoardConfig(
                name="15M",
                source_kind="slug_prefix",
                slug_prefixes=("btc-updown-15m", "eth-updown-15m", "sol-updown-15m"),
                slug_interval_minutes=15,
                slug_format="timestamp",
                max_games=30,
            ),
        ),
        (
            "1H",
            BoardConfig(
                name="1H",
                source_kind="slug_prefix",
                slug_prefixes=("bitcoin-up-or-down", "ethereum-up-or-down", "solana-up-or-down"),
                slug_format="hourly-et",
                max_games=30,
            ),
        ),
        (
            "NHL",
            BoardConfig(name="NHL", source_kind="tag", sport="nhl", tag_id=899, related_tags=True, max_games=30),
        ),
        (
            "CBB",
            BoardConfig(name="CBB", source_kind="tag", sport="college-basketball", tag_id=101178, related_tags=True, max_games=30),
        ),
        (
            "MLB",
            BoardConfig(name="MLB", source_kind="tag", sport="mlb", tag_id=100381, related_tags=True, max_games=30),
        ),
        (
            "CRICKET",
            BoardConfig(name="CRICKET", source_kind="tag", sport="cricket", tag_id=101977, related_tags=True, max_games=30),
        ),
    )
)


def board_names() -> List[str]:
    return list(BOARD_CATALOG.keys())


def get_board(name: str) -> BoardConfig:
    key = str(name or "").strip().upper()
    if key not in BOARD_CATALOG:
        raise ValueError(f"unknown board: {name}")
    return BOARD_CATALOG[key]


def normalize_board_names(names: Sequence[str]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for name in names:
        key = str(name or "").strip().upper()
        if not key:
            continue
        if key not in BOARD_CATALOG:
            raise ValueError(f"unknown board: {name}")
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    if not out:
        raise ValueError("at least one board is required")
    return out
