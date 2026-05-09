from xuanshu.sizing.position_sizer import OpenOrderSizingInput, calculate_open_order_size


def test_position_sizer_shrinks_order_to_available_margin() -> None:
    result = calculate_open_order_size(
        OpenOrderSizingInput(
            symbol="ETH-USDT-SWAP",
            requested_size=2.3345,
            mark_price=2396.85,
            equity=667.0,
            available_balance=119.29,
            starting_nav=667.0,
            max_leverage=3,
        )
    )

    assert result.block_reason is None
    assert result.order_size == 1.41


def test_position_sizer_does_not_cap_single_symbol_by_target_margin_budget() -> None:
    result = calculate_open_order_size(
        OpenOrderSizingInput(
            symbol="BTC-USDT-SWAP",
            requested_size=28.93,
            mark_price=80795.1,
            equity=964.3938,
            available_balance=675.3113,
            starting_nav=964.3938,
            max_leverage=3,
        )
    )

    assert result.block_reason is None
    assert result.order_size == 2.38


def test_position_sizer_blocks_when_adjusted_size_is_below_minimum() -> None:
    result = calculate_open_order_size(
        OpenOrderSizingInput(
            symbol="ETH-USDT-SWAP",
            requested_size=2.3345,
            mark_price=2396.85,
            equity=667.0,
            available_balance=50.0,
            starting_nav=667.0,
            max_leverage=3,
        )
    )

    assert result.block_reason == "insufficient_available_margin"
    assert result.order_size == 0.0
