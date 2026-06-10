from public_copytrade_cli.main import (
    PriceLookupContext,
    TradeEvent,
    build_output_payload,
    fetch_token_price_info,
    generate_strategies,
    parse_args,
)


def make_result(
    strategy: str,
    *,
    copy_mode: str,
    fixed_usd,
    proportional_pct,
    proportional_cap_usd,
    max_entries_per_market: int,
    total_pnl: float,
    roi: float,
    total_buy_cost: float,
    copied_buys: int,
    mirrored_sells: int,
):
    return {
        "strategy": strategy,
        "copy_mode": copy_mode,
        "fixed_usd": fixed_usd,
        "proportional_pct": proportional_pct,
        "proportional_cap_usd": proportional_cap_usd,
        "max_entries_per_market": max_entries_per_market,
        "total_pnl": total_pnl,
        "roi": roi,
        "total_buy_cost": total_buy_cost,
        "copied_buys": copied_buys,
        "mirrored_sells": mirrored_sells,
    }


def test_generate_strategies_count():
    strategies = generate_strategies([5.0, 20.0, 50.0, 100.0], [0.005, 0.01, 0.03, 0.05], [5.0, 20.0, 50.0, 100.0], 20)
    assert len(strategies) == 400


def test_parse_args_defaults_and_custom_values():
    defaults = parse_args(["--address", "0xabc"])
    assert defaults.address == "0xabc"
    assert defaults.max_activities == 50000
    assert defaults.premium == 0.03
    assert defaults.mirror_sell_slippage == 0.01

    custom = parse_args(
        [
            "--address",
            "0xdef",
            "--max-activities",
            "123",
            "--premium",
            "0.02",
            "--mirror-sell-slippage",
            "0.015",
        ]
    )
    assert custom.max_activities == 123
    assert custom.premium == 0.02
    assert custom.mirror_sell_slippage == 0.015


def test_build_output_payload_shape_and_rankings():
    events = [
        TradeEvent("tx1", 1000, "BUY", "tok1", "m1", "slug-1", 0.5, 10.0, 5.0),
        TradeEvent("tx2", 1000 + 86400, "SELL", "tok1", "m1", "slug-1", 0.6, 10.0, 6.0),
    ]
    replay_events = list(events)
    results = [
        make_result(
            "s1",
            copy_mode="fixed_usd",
            fixed_usd=5.0,
            proportional_pct=0.0,
            proportional_cap_usd=None,
            max_entries_per_market=1,
            total_pnl=100.0,
            roi=0.20,
            total_buy_cost=500.0,
            copied_buys=5,
            mirrored_sells=2,
        ),
        make_result(
            "s2",
            copy_mode="fixed_usd",
            fixed_usd=20.0,
            proportional_pct=0.0,
            proportional_cap_usd=None,
            max_entries_per_market=2,
            total_pnl=150.0,
            roi=0.15,
            total_buy_cost=1000.0,
            copied_buys=7,
            mirrored_sells=3,
        ),
        make_result(
            "s3",
            copy_mode="proportional",
            fixed_usd=None,
            proportional_pct=0.01,
            proportional_cap_usd=20.0,
            max_entries_per_market=3,
            total_pnl=120.0,
            roi=0.20,
            total_buy_cost=600.0,
            copied_buys=6,
            mirrored_sells=3,
        ),
    ]

    payload = build_output_payload(
        address="0xabc",
        max_activities=50000,
        premium=0.03,
        mirror_sell_slippage=0.01,
        events=events,
        replay_events=replay_events,
        benchmark={"actual_window_pnl_delta": 42.5},
        buy_signal_stats={
            "avg_bets_per_market": 2.5,
            "avg_usd_per_market": 120.0,
            "buy_signal_count": 9,
            "aggregated_buy_signal_count": 3,
        },
        results=results,
    )

    assert set(payload.keys()) == {"generated_at", "input", "summary", "best_returns", "top_strategies"}
    assert payload["summary"]["trade_count"] == payload["summary"]["fetched_events"] == 2
    assert payload["summary"]["backtest_span_days"] == 1.0
    assert payload["best_returns"]["best_by_roi"]["strategy"] == "s3"
    assert payload["best_returns"]["best_by_total_pnl"]["strategy"] == "s2"
    assert payload["top_strategies"]["top5_by_roi"][0] == payload["best_returns"]["best_by_roi"]
    assert payload["top_strategies"]["top5_by_total_pnl"][0] == payload["best_returns"]["best_by_total_pnl"]
    assert "window_analysis" not in payload
    assert "ai_improvement_summary" not in payload


def test_build_output_payload_benchmark_error():
    events = [TradeEvent("tx1", 1000, "BUY", "tok1", "m1", "slug-1", 0.5, 10.0, 5.0)]
    results = [
        make_result(
            "s1",
            copy_mode="fixed_usd",
            fixed_usd=5.0,
            proportional_pct=0.0,
            proportional_cap_usd=None,
            max_entries_per_market=1,
            total_pnl=10.0,
            roi=0.10,
            total_buy_cost=100.0,
            copied_buys=1,
            mirrored_sells=0,
        )
    ]

    payload = build_output_payload(
        address="0xabc",
        max_activities=100,
        premium=0.03,
        mirror_sell_slippage=0.01,
        events=events,
        replay_events=events,
        benchmark={"actual_window_pnl_delta": None, "error": "benchmark unavailable"},
        buy_signal_stats={
            "avg_bets_per_market": 1.0,
            "avg_usd_per_market": 5.0,
            "buy_signal_count": 1,
            "aggregated_buy_signal_count": 0,
        },
        results=results,
    )

    assert payload["summary"]["window_real_pnl"] is None
    assert payload["summary"]["benchmark_error"] == "benchmark unavailable"


def test_fetch_token_price_info_falls_back_to_event_resolution(monkeypatch):
    responses = [
        [],
        RuntimeError("midpoint unavailable"),
        [
            {
                "slug": "nba-sas-okc-2026-05-26",
                "markets": [
                    {
                        "conditionId": "0xb8724de28f0dcdb71b55abc89f54a5788f7d7fc364160ec6a45b941d5c332988",
                        "slug": "nba-sas-okc-2026-05-26",
                        "closed": True,
                        "clobTokenIds": [
                            "53919346162802748120847155404148342783978231188785990665971474884640857460504",
                            "45942712780956716290595528630059826510539112426790184133339143529925294470101",
                        ],
                        "outcomePrices": ["0", "1"],
                    }
                ],
            }
        ],
    ]

    def fake_http_get_json(*args, **kwargs):
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr("public_copytrade_cli.main.http_get_json", fake_http_get_json)
    info = fetch_token_price_info(
        object(),
        "45942712780956716290595528630059826510539112426790184133339143529925294470101",
        timeout_s=1.0,
        lookup_context=PriceLookupContext(
            market_slug="nba-sas-okc-2026-05-26",
            condition_id="0xb8724de28f0dcdb71b55abc89f54a5788f7d7fc364160ec6a45b941d5c332988",
        ),
    )

    assert info.price == 1.0
    assert info.resolved is True
    assert info.source == "resolution"
