"""Shared signal aggregation helpers for live trading and analytics."""

from __future__ import annotations

import time
from typing import Any, Dict, List, MutableMapping, Optional, Tuple

from copytrade.config import (
    AGGREGATION_MODE_EXECUTION_EPISODE,
    AGGREGATION_MODE_STRICT_PRICE,
    CopyTradeConfig,
)
from copytrade.monitor import LeaderTrade


def compute_maker_like_score(
    *,
    count: int,
    span_s: int,
    max_piece_usd: float,
    min_trade_size_usd: float,
    window_s: int,
) -> float:
    frag = min(1.0, max(0.0, (count - 1) / 4.0))
    piece_ratio = max_piece_usd / max(min_trade_size_usd, 1e-9)
    small_piece = 1.0 - min(1.0, piece_ratio)
    continuity = 1.0 - min(1.0, span_s / max(window_s, 1))
    return max(0.0, min(1.0, 0.45 * frag + 0.35 * small_piece + 0.20 * continuity))


def compute_vwap_price(
    *,
    cum_usd: float,
    cum_size: float,
    price_sum: float = 0.0,
    count: int = 0,
) -> float:
    if cum_size > 1e-12:
        return float(cum_usd) / float(cum_size)
    if count > 0:
        return float(price_sum) / max(1, int(count))
    return 0.0


def execution_episode_price_band(anchor_price: float, abs_band: float, bps_band: float) -> float:
    anchor = max(float(anchor_price or 0.0), 0.0)
    return max(float(abs_band or 0.0), anchor * float(bps_band or 0.0) / 10000.0)


def get_effective_signal_price(trade: Any) -> Optional[float]:
    hint = _safe_positive_float(getattr(trade, "execution_price_hint", None))
    if hint is not None:
        return hint
    return _safe_positive_float(getattr(trade, "price", None))


def prepare_copy_signals_live(
    new_trades: List[LeaderTrade],
    cfg: CopyTradeConfig,
    states: MutableMapping[Tuple[Any, ...], Dict[str, Any]],
) -> List[LeaderTrade]:
    if not new_trades:
        return []

    out: List[LeaderTrade] = []
    ordered = sorted(new_trades, key=lambda t: (t.ts_int or 0, t.tx_hash or ""))
    _cleanup_live_states(states, int(time.time()), cfg)

    for lt in ordered:
        if lt.side != "BUY":
            continue

        lcfg = cfg.get_leader_config(lt.leader_address)
        usd = _safe_positive_float(lt.usd_amount)
        if usd is None or usd >= float(lcfg.min_trade_size_usd) or not bool(lcfg.maker_like_enabled):
            out.append(lt)
            continue

        price = _safe_positive_float(lt.price)
        if not lt.token_id or price is None:
            out.append(lt)
            continue

        mode = _normalized_mode(getattr(lcfg, "aggregation_mode", AGGREGATION_MODE_STRICT_PRICE))
        if mode == AGGREGATION_MODE_EXECUTION_EPISODE:
            _process_execution_episode_live(lt, lcfg, states, out)
        else:
            _process_strict_price_live(lt, lcfg, states, out)

    return out


def aggregate_trade_dicts_offline(
    trades: List[Dict[str, Any]],
    *,
    min_trade_size_usd: float = 500.0,
    window_minutes: float = 360.0,
    max_gap_minutes: float = 30.0,
    score_threshold: float = 0.60,
    enabled: bool = True,
    aggregation_mode: str = AGGREGATION_MODE_STRICT_PRICE,
    execution_episode_window_minutes: float = 20.0,
    execution_episode_max_gap_minutes: float = 5.0,
    execution_episode_price_band_abs: float = 0.03,
    execution_episode_price_band_bps: float = 500.0,
    execution_episode_min_fill_count: int = 2,
) -> List[Dict[str, Any]]:
    if not trades or not enabled:
        return list(trades)

    mode = _normalized_mode(aggregation_mode)
    strict_window_s = max(1, int(window_minutes * 60))
    strict_max_gap_s = max(1, int(max_gap_minutes * 60))
    episode_window_s = max(1, int(execution_episode_window_minutes * 60))
    episode_max_gap_s = max(1, int(execution_episode_max_gap_minutes * 60))
    episode_min_fill_count = max(1, int(execution_episode_min_fill_count or 1))

    sorted_trades = sorted(trades, key=lambda t: (t.get("ts_epoch") or 0, t.get("tx_hash") or ""))
    states: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
    result: List[Dict[str, Any]] = []

    for trade in sorted_trades:
        side = str(trade.get("side") or "")
        if side != "BUY":
            result.append(trade)
            continue

        usd = _safe_positive_float(trade.get("usd"))
        if usd is None or usd >= float(min_trade_size_usd):
            result.append(trade)
            continue

        token_id = str(trade.get("token_id") or "")
        price = _safe_positive_float(trade.get("price"))
        if not token_id or price is None:
            result.append(trade)
            continue

        if mode == AGGREGATION_MODE_EXECUTION_EPISODE:
            key = _offline_execution_episode_key(trade)
            st = states.get(key)
            if st is None:
                states[key] = _new_offline_state(
                    trade,
                    aggregation_mode=mode,
                    window_s=episode_window_s,
                    max_gap_s=episode_max_gap_s,
                )
                continue

            if not _should_join_execution_episode_state(
                st,
                ts=int(trade.get("ts_epoch") or 0),
                price=price,
                tx_hash=str(trade.get("tx_hash") or ""),
                max_gap_s=episode_max_gap_s,
                window_s=episode_window_s,
                abs_band=execution_episode_price_band_abs,
                bps_band=execution_episode_price_band_bps,
            ):
                _flush_offline_state(result, st)
                states[key] = _new_offline_state(
                    trade,
                    aggregation_mode=mode,
                    window_s=episode_window_s,
                    max_gap_s=episode_max_gap_s,
                )
                continue

            _append_offline_state(st, trade)
            if (
                not bool(st.get("emitted"))
                and int(st.get("count", 0)) >= episode_min_fill_count
                and float(st.get("cum_usd", 0.0)) >= float(min_trade_size_usd)
            ):
                result.append(_build_offline_aggregated_trade(st, aggregation_mode=mode))
                st["emitted"] = True
            continue

        key = _offline_strict_price_key(trade)
        st = states.get(key)
        if st is None:
            states[key] = _new_offline_state(
                trade,
                aggregation_mode=mode,
                window_s=strict_window_s,
                max_gap_s=strict_max_gap_s,
                score_threshold=score_threshold,
            )
            continue

        ts = int(trade.get("ts_epoch") or 0)
        too_far = ts - int(st["last_ts"]) > strict_max_gap_s
        out_of_window = ts - int(st["first_ts"]) > strict_window_s
        if too_far or out_of_window:
            _flush_offline_state(result, st)
            states[key] = _new_offline_state(
                trade,
                aggregation_mode=mode,
                window_s=strict_window_s,
                max_gap_s=strict_max_gap_s,
                score_threshold=score_threshold,
            )
            continue

        _append_offline_state(st, trade)
        if float(st["cum_usd"]) < float(min_trade_size_usd):
            continue

        score = compute_maker_like_score(
            count=int(st["count"]),
            span_s=max(1, int(st["last_ts"]) - int(st["first_ts"])),
            max_piece_usd=float(st["max_piece_usd"]),
            min_trade_size_usd=float(min_trade_size_usd),
            window_s=strict_window_s,
        )
        if score < float(score_threshold):
            continue

        result.append(_build_offline_aggregated_trade(st, aggregation_mode=mode, score=score))
        states.pop(key, None)

    for st in states.values():
        _flush_offline_state(result, st)

    result.sort(key=lambda t: (t.get("ts_epoch") or 0, t.get("tx_hash") or ""))
    return result


def _normalized_mode(value: Any) -> str:
    text = str(value or AGGREGATION_MODE_STRICT_PRICE).strip().lower()
    if text == AGGREGATION_MODE_EXECUTION_EPISODE:
        return AGGREGATION_MODE_EXECUTION_EPISODE
    return AGGREGATION_MODE_STRICT_PRICE


def _safe_positive_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except Exception:
        return None
    if number <= 0:
        return None
    return number


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _cleanup_live_states(
    states: MutableMapping[Tuple[Any, ...], Dict[str, Any]],
    now_ts: int,
    cfg: CopyTradeConfig,
) -> None:
    stale_keys = []
    default_window_s = max(1, int(cfg.maker_like_window_minutes * 60))
    for key, state in states.items():
        state_window_s = max(1, int(state.get("window_s", default_window_s)))
        if int(state.get("last_ts", 0)) < now_ts - state_window_s:
            stale_keys.append(key)
    for key in stale_keys:
        states.pop(key, None)


def _process_strict_price_live(
    trade: LeaderTrade,
    cfg: CopyTradeConfig,
    states: MutableMapping[Tuple[Any, ...], Dict[str, Any]],
    out: List[LeaderTrade],
) -> None:
    ts = int(trade.ts_int) if isinstance(trade.ts_int, int) else int(time.time())
    key = _live_strict_price_key(trade)
    window_s = max(1, int(cfg.maker_like_window_minutes * 60))
    max_gap_s = max(1, int(cfg.maker_like_max_gap_minutes * 60))
    score_threshold = float(cfg.maker_like_score_threshold)

    st = states.get(key)
    if st is None:
        states[key] = _new_live_state(
            trade,
            aggregation_mode=AGGREGATION_MODE_STRICT_PRICE,
            ts=ts,
            window_s=window_s,
            max_gap_s=max_gap_s,
            score_threshold=score_threshold,
        )
        return

    too_far = ts - int(st["last_ts"]) > max_gap_s
    out_of_window = ts - int(st["first_ts"]) > window_s
    if too_far or out_of_window:
        states[key] = _new_live_state(
            trade,
            aggregation_mode=AGGREGATION_MODE_STRICT_PRICE,
            ts=ts,
            window_s=window_s,
            max_gap_s=max_gap_s,
            score_threshold=score_threshold,
        )
        return

    _append_live_state(st, trade, ts=ts)
    if float(st["cum_usd"]) < float(cfg.min_trade_size_usd):
        return

    score = compute_maker_like_score(
        count=int(st["count"]),
        span_s=max(1, int(st["last_ts"]) - int(st["first_ts"])),
        max_piece_usd=float(st["max_piece_usd"]),
        min_trade_size_usd=float(cfg.min_trade_size_usd),
        window_s=window_s,
    )
    if score < score_threshold:
        return

    out.append(_build_live_aggregated_trade(trade, st, aggregation_mode=AGGREGATION_MODE_STRICT_PRICE, score=score))
    states.pop(key, None)


def _process_execution_episode_live(
    trade: LeaderTrade,
    cfg: CopyTradeConfig,
    states: MutableMapping[Tuple[Any, ...], Dict[str, Any]],
    out: List[LeaderTrade],
) -> None:
    ts = int(trade.ts_int) if isinstance(trade.ts_int, int) else int(time.time())
    key = _live_execution_episode_key(trade)
    window_s = max(1, int(cfg.execution_episode_window_minutes * 60))
    max_gap_s = max(1, int(cfg.execution_episode_max_gap_minutes * 60))
    min_fill_count = max(1, int(cfg.execution_episode_min_fill_count or 1))
    abs_band = float(cfg.execution_episode_price_band_abs)
    bps_band = float(cfg.execution_episode_price_band_bps)

    st = states.get(key)
    if st is None:
        states[key] = _new_live_state(
            trade,
            aggregation_mode=AGGREGATION_MODE_EXECUTION_EPISODE,
            ts=ts,
            window_s=window_s,
            max_gap_s=max_gap_s,
        )
        return

    if not _should_join_execution_episode_state(
        st,
        ts=ts,
        price=float(trade.price),
        tx_hash=trade.tx_hash,
        max_gap_s=max_gap_s,
        window_s=window_s,
        abs_band=abs_band,
        bps_band=bps_band,
    ):
        states[key] = _new_live_state(
            trade,
            aggregation_mode=AGGREGATION_MODE_EXECUTION_EPISODE,
            ts=ts,
            window_s=window_s,
            max_gap_s=max_gap_s,
        )
        return

    _append_live_state(st, trade, ts=ts)
    if bool(st.get("emitted")):
        return
    if int(st.get("count", 0)) < min_fill_count:
        return
    if float(st.get("cum_usd", 0.0)) < float(cfg.min_trade_size_usd):
        return

    out.append(_build_live_aggregated_trade(trade, st, aggregation_mode=AGGREGATION_MODE_EXECUTION_EPISODE))
    st["emitted"] = True


def _should_join_execution_episode_state(
    state: Dict[str, Any],
    *,
    ts: int,
    price: float,
    tx_hash: str,
    max_gap_s: int,
    window_s: int,
    abs_band: float,
    bps_band: float,
) -> bool:
    last_tx = str(state.get("last_tx") or "")
    if tx_hash and last_tx and tx_hash == last_tx:
        return True

    if ts - int(state["last_ts"]) > max_gap_s:
        return False
    if ts - int(state["first_ts"]) > window_s:
        return False

    last_price = float(state.get("last_price") or 0.0)
    vwap_price = compute_vwap_price(
        cum_usd=float(state.get("cum_usd", 0.0)),
        cum_size=float(state.get("cum_size", 0.0)),
        price_sum=float(state.get("price_sum", 0.0)),
        count=int(state.get("count", 0)),
    )
    if abs(price - last_price) > execution_episode_price_band(last_price, abs_band, bps_band):
        return False
    if abs(price - vwap_price) > execution_episode_price_band(vwap_price, abs_band, bps_band):
        return False
    return True


def _new_live_state(
    trade: LeaderTrade,
    *,
    aggregation_mode: str,
    ts: int,
    window_s: int,
    max_gap_s: int,
    score_threshold: Optional[float] = None,
) -> Dict[str, Any]:
    price = float(trade.price)
    usd = float(trade.usd_amount)
    return {
        "aggregation_mode": aggregation_mode,
        "first_ts": ts,
        "last_ts": ts,
        "cum_usd": usd,
        "cum_size": _safe_float(trade.size),
        "count": 1,
        "price_sum": price,
        "last_price": price,
        "max_piece_usd": usd,
        "window_s": window_s,
        "max_gap_s": max_gap_s,
        "score_threshold": score_threshold,
        "last_slug": trade.market_slug,
        "last_tx": trade.tx_hash,
        "emitted": False,
    }


def _append_live_state(st: Dict[str, Any], trade: LeaderTrade, *, ts: int) -> None:
    price = float(trade.price)
    usd = float(trade.usd_amount)
    st["last_ts"] = ts
    st["cum_usd"] = float(st["cum_usd"]) + usd
    st["cum_size"] = float(st["cum_size"]) + _safe_float(trade.size)
    st["count"] = int(st["count"]) + 1
    st["price_sum"] = float(st["price_sum"]) + price
    st["last_price"] = price
    st["max_piece_usd"] = max(float(st["max_piece_usd"]), usd)
    st["last_slug"] = trade.market_slug
    st["last_tx"] = trade.tx_hash


def _build_live_aggregated_trade(
    trade: LeaderTrade,
    state: Dict[str, Any],
    *,
    aggregation_mode: str,
    score: Optional[float] = None,
) -> LeaderTrade:
    count = int(state["count"])
    vwap_price = compute_vwap_price(
        cum_usd=float(state["cum_usd"]),
        cum_size=float(state["cum_size"]),
        price_sum=float(state["price_sum"]),
        count=count,
    )
    agg_tx = f"agg-{trade.leader_address[:10]}-{trade.token_id[:12]}-{int(state['last_ts'])}-{count}"
    return LeaderTrade(
        leader_address=trade.leader_address,
        tx_hash=agg_tx,
        fill_key=f"agg:{agg_tx}",
        timestamp=str(state["last_ts"]),
        side="BUY",
        token_id=trade.token_id,
        condition_id=trade.condition_id,
        price=(vwap_price if vwap_price > 0 else trade.price),
        size=(float(state["cum_size"]) if float(state["cum_size"]) > 0 else None),
        usd_amount=float(state["cum_usd"]),
        outcome=trade.outcome,
        market_slug=state.get("last_slug") or trade.market_slug,
        ts_int=int(state["last_ts"]),
        is_maker_like_aggregated=True,
        maker_like_score=score,
        execution_price_hint=float(state["last_price"]) if aggregation_mode == AGGREGATION_MODE_EXECUTION_EPISODE else None,
        aggregation_source_count=count,
        aggregation_kind=aggregation_mode,
        source=str(getattr(trade, "source", "activity") or "activity"),
    )


def _live_strict_price_key(trade: LeaderTrade) -> Tuple[Any, ...]:
    return (
        trade.leader_address.lower(),
        trade.token_id,
        trade.condition_id or "",
        trade.outcome or "",
        round(float(trade.price), 4),
    )


def _live_execution_episode_key(trade: LeaderTrade) -> Tuple[Any, ...]:
    return (
        trade.leader_address.lower(),
        trade.token_id,
        trade.condition_id or "",
        trade.outcome or "",
        trade.side,
    )


def _new_offline_state(
    trade: Dict[str, Any],
    *,
    aggregation_mode: str,
    window_s: int,
    max_gap_s: int,
    score_threshold: Optional[float] = None,
) -> Dict[str, Any]:
    price = float(trade.get("price") or 0.0)
    usd = float(trade.get("usd") or 0.0)
    ts = int(trade.get("ts_epoch") or 0)
    return {
        "aggregation_mode": aggregation_mode,
        "first_ts": ts,
        "last_ts": ts,
        "cum_usd": usd,
        "cum_size": _safe_float(trade.get("size")),
        "count": 1,
        "price_sum": price,
        "last_price": price,
        "max_piece_usd": usd,
        "window_s": window_s,
        "max_gap_s": max_gap_s,
        "score_threshold": score_threshold,
        "last_slug": trade.get("market_slug"),
        "last_tx": trade.get("tx_hash"),
        "source": str(trade.get("source") or "activity"),
        "leader_address": trade.get("leader_address", ""),
        "token_id": trade.get("token_id"),
        "condition_id": trade.get("condition_id"),
        "outcome": trade.get("outcome"),
        "emitted": False,
        "_originals": [trade],
    }


def _append_offline_state(st: Dict[str, Any], trade: Dict[str, Any]) -> None:
    price = float(trade.get("price") or 0.0)
    usd = float(trade.get("usd") or 0.0)
    st["last_ts"] = int(trade.get("ts_epoch") or 0)
    st["cum_usd"] = float(st["cum_usd"]) + usd
    st["cum_size"] = float(st["cum_size"]) + _safe_float(trade.get("size"))
    st["count"] = int(st["count"]) + 1
    st["price_sum"] = float(st["price_sum"]) + price
    st["last_price"] = price
    st["max_piece_usd"] = max(float(st["max_piece_usd"]), usd)
    st["last_slug"] = trade.get("market_slug")
    st["last_tx"] = trade.get("tx_hash")
    st["_originals"].append(trade)


def _build_offline_aggregated_trade(
    state: Dict[str, Any],
    *,
    aggregation_mode: str,
    score: Optional[float] = None,
) -> Dict[str, Any]:
    count = int(state["count"])
    vwap_price = compute_vwap_price(
        cum_usd=float(state["cum_usd"]),
        cum_size=float(state["cum_size"]),
        price_sum=float(state["price_sum"]),
        count=count,
    )
    agg_tx = (
        f"agg-{str(state.get('leader_address', ''))[:10]}-"
        f"{str(state.get('token_id', ''))[:12]}-"
        f"{int(state['last_ts'])}-{count}"
    )
    out = {
        "tx_hash": agg_tx,
        "leader_address": state.get("leader_address", ""),
        "timestamp_utc": str(state["last_ts"]),
        "ts_epoch": int(state["last_ts"]),
        "side": "BUY",
        "token_id": state.get("token_id"),
        "condition_id": state.get("condition_id"),
        "market_slug": state.get("last_slug"),
        "outcome": state.get("outcome"),
        "price": vwap_price,
        "size": float(state["cum_size"]),
        "usd": float(state["cum_usd"]),
        "_is_aggregated": True,
        "_source_count": count,
        "_aggregation_kind": aggregation_mode,
        "source": state.get("source") or "activity",
    }
    if aggregation_mode == AGGREGATION_MODE_EXECUTION_EPISODE:
        out["_execution_price_hint"] = float(state["last_price"])
    if score is not None:
        out["_maker_like_score"] = score
    return out


def _flush_offline_state(result: List[Dict[str, Any]], state: Dict[str, Any]) -> None:
    if bool(state.get("emitted")):
        return
    result.extend(state.get("_originals", []))


def _offline_strict_price_key(trade: Dict[str, Any]) -> Tuple[Any, ...]:
    return (
        str(trade.get("leader_address") or "").lower(),
        trade.get("token_id") or "",
        trade.get("condition_id") or "",
        trade.get("outcome") or "",
        round(float(trade.get("price") or 0.0), 4),
    )


def _offline_execution_episode_key(trade: Dict[str, Any]) -> Tuple[Any, ...]:
    return (
        str(trade.get("leader_address") or "").lower(),
        trade.get("token_id") or "",
        trade.get("condition_id") or "",
        trade.get("outcome") or "",
        str(trade.get("side") or ""),
    )
