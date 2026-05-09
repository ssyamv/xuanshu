from __future__ import annotations

from dataclasses import dataclass


MARGIN_USAGE_BUFFER = 0.80
CONTRACT_VALUE_BY_SYMBOL = {
    "BTC-USDT-SWAP": 0.01,
    "ETH-USDT-SWAP": 0.1,
}
LOT_SIZE_BY_SYMBOL = {
    "BTC-USDT-SWAP": 0.01,
    "ETH-USDT-SWAP": 0.01,
}
PORTFOLIO_MARGIN_FRACTION_PER_SYMBOL = 0.5


@dataclass(frozen=True, slots=True)
class OpenOrderSizingInput:
    symbol: str
    requested_size: float
    mark_price: float
    equity: float
    available_balance: float
    starting_nav: float
    max_leverage: float


@dataclass(frozen=True, slots=True)
class OpenOrderSizingResult:
    order_size: float
    block_reason: str | None
    target_margin_budget: float | None = None
    target_notional: float | None = None
    max_affordable_size: float | None = None
    margin_per_contract: float | None = None


def calculate_open_order_size(sizing_input: OpenOrderSizingInput) -> OpenOrderSizingResult:
    requested_size = sizing_input.requested_size
    if requested_size <= 0.0:
        return OpenOrderSizingResult(order_size=0.0, block_reason="non_positive_requested_size")

    if sizing_input.equity <= 0.0 and sizing_input.available_balance <= 0.0:
        return OpenOrderSizingResult(order_size=requested_size, block_reason=None)

    if sizing_input.available_balance <= 0.0:
        return OpenOrderSizingResult(order_size=0.0, block_reason="insufficient_available_margin")

    mark_price = sizing_input.mark_price
    contract_value = CONTRACT_VALUE_BY_SYMBOL.get(sizing_input.symbol)
    if contract_value is None or mark_price <= 0.0:
        return OpenOrderSizingResult(order_size=requested_size, block_reason=None)

    max_leverage = max(float(sizing_input.max_leverage), 1.0)
    margin_per_contract = mark_price * contract_value / max_leverage
    if margin_per_contract <= 0.0:
        return OpenOrderSizingResult(order_size=requested_size, block_reason=None)

    target_margin_budget = max(sizing_input.equity, sizing_input.starting_nav) * PORTFOLIO_MARGIN_FRACTION_PER_SYMBOL
    target_notional = target_margin_budget * max_leverage
    target_size = target_notional / (mark_price * contract_value)
    max_affordable_size = sizing_input.available_balance * MARGIN_USAGE_BUFFER / margin_per_contract
    lot_size = LOT_SIZE_BY_SYMBOL.get(sizing_input.symbol, 0.01)
    adjusted_size = _floor_to_lot_size(min(requested_size, target_size, max_affordable_size), lot_size)
    if adjusted_size < 1.0:
        return OpenOrderSizingResult(
            order_size=0.0,
            block_reason="insufficient_available_margin",
            target_margin_budget=target_margin_budget,
            target_notional=target_notional,
            max_affordable_size=max_affordable_size,
            margin_per_contract=margin_per_contract,
        )

    return OpenOrderSizingResult(
        order_size=adjusted_size,
        block_reason=None,
        target_margin_budget=target_margin_budget,
        target_notional=target_notional,
        max_affordable_size=max_affordable_size,
        margin_per_contract=margin_per_contract,
    )


def _floor_to_lot_size(size: float, lot_size: float) -> float:
    if lot_size <= 0.0:
        return size
    return (size // lot_size) * lot_size
