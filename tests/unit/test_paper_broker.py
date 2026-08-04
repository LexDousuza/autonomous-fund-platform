"""
Unit tests for core.broker.paper's client_order_id idempotency behavior.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_paper_broker.py -v
"""
from __future__ import annotations

import unittest

from core.broker.base import OptionType, OrderRequest, OrderSide, OrderType, PositionEffect, ProductType
from core.broker.paper import PaperBroker


def _order_request(client_order_id: str | None = None, quantity: int = 10) -> OrderRequest:
    return OrderRequest(
        symbol="RELIANCE",
        exchange="NSE",
        side=OrderSide.BUY,
        quantity=quantity,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
        price=2500.0,
        client_order_id=client_order_id,
    )


def _naked_write_open_request(quantity: int = 50) -> OrderRequest:
    return OrderRequest(
        symbol="NIFTY26AUG23000CE",
        exchange="NFO",
        side=OrderSide.SELL,
        quantity=quantity,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
        price=120.0,
        strike=23000.0,
        expiry="2026-08-27",
        option_type=OptionType.CALL,
        lot_size=quantity,
        position_effect=PositionEffect.OPEN,
    )


class PaperBrokerClientOrderIdTests(unittest.TestCase):
    def test_no_client_order_id_creates_a_new_fill_every_time(self) -> None:
        broker = PaperBroker()

        first = broker.place_order(_order_request())
        second = broker.place_order(_order_request())

        self.assertNotEqual(first.order_id, second.order_id)
        self.assertEqual(broker.list_positions()[0].quantity, 20)  # both fills applied

    def test_repeated_client_order_id_returns_the_original_order(self) -> None:
        broker = PaperBroker()

        first = broker.place_order(_order_request(client_order_id="idem-1"))
        second = broker.place_order(_order_request(client_order_id="idem-1"))

        self.assertEqual(first.order_id, second.order_id)
        self.assertIs(first, second)

    def test_repeated_client_order_id_does_not_double_apply_to_positions(self) -> None:
        broker = PaperBroker()

        broker.place_order(_order_request(client_order_id="idem-1"))
        broker.place_order(_order_request(client_order_id="idem-1"))

        self.assertEqual(broker.list_positions()[0].quantity, 10)  # only the first fill counted

    def test_different_client_order_ids_both_create_real_fills(self) -> None:
        broker = PaperBroker()

        broker.place_order(_order_request(client_order_id="idem-1"))
        broker.place_order(_order_request(client_order_id="idem-2"))

        self.assertEqual(broker.list_positions()[0].quantity, 20)


class PaperBrokerSlippageTests(unittest.TestCase):
    def test_zero_slippage_fills_at_exact_requested_price(self) -> None:
        broker = PaperBroker()
        order = broker.place_order(_order_request())
        self.assertEqual(order.average_price, 2500.0)

    def test_buy_fills_above_requested_price(self) -> None:
        broker = PaperBroker(slippage_bps=10.0)  # 0.10%
        order = broker.place_order(_order_request())
        self.assertAlmostEqual(order.average_price, 2500.0 * 1.001)

    def test_sell_fills_below_requested_price(self) -> None:
        broker = PaperBroker(slippage_bps=10.0)
        request = OrderRequest(
            symbol="RELIANCE",
            exchange="NSE",
            side=OrderSide.SELL,
            quantity=10,
            order_type=OrderType.MARKET,
            product_type=ProductType.INTRADAY,
            price=2500.0,
        )
        order = broker.place_order(request)
        self.assertAlmostEqual(order.average_price, 2500.0 * 0.999)

    def test_zero_or_missing_price_is_never_slipped(self) -> None:
        broker = PaperBroker(slippage_bps=10.0)
        request = OrderRequest(
            symbol="RELIANCE",
            exchange="NSE",
            side=OrderSide.BUY,
            quantity=10,
            order_type=OrderType.MARKET,
            product_type=ProductType.INTRADAY,
            price=None,
        )
        order = broker.place_order(request)
        self.assertEqual(order.average_price, 0.0)


class PaperBrokerOptionsFieldPassthroughTests(unittest.TestCase):
    """
    strike/expiry/option_type/lot_size/position_effect must survive a
    paper fill - PaperBroker previously dropped all five, silently
    turning every paper options fill into an indistinguishable-from-cash
    Order/Position, which would defeat any options-strategy paper test.
    """

    def test_filled_order_carries_options_fields(self) -> None:
        broker = PaperBroker()

        order = broker.place_order(_naked_write_open_request())

        self.assertEqual(order.strike, 23000.0)
        self.assertEqual(order.expiry, "2026-08-27")
        self.assertEqual(order.option_type, OptionType.CALL)
        self.assertEqual(order.lot_size, 50)
        self.assertEqual(order.position_effect, PositionEffect.OPEN)

    def test_new_position_carries_options_fields(self) -> None:
        broker = PaperBroker()

        broker.place_order(_naked_write_open_request())
        position = broker.list_positions()[0]

        self.assertEqual(position.strike, 23000.0)
        self.assertEqual(position.expiry, "2026-08-27")
        self.assertEqual(position.option_type, OptionType.CALL)
        self.assertEqual(position.lot_size, 50)

    def test_accumulating_same_direction_position_keeps_options_fields(self) -> None:
        broker = PaperBroker()

        broker.place_order(_naked_write_open_request(quantity=50))
        broker.place_order(_naked_write_open_request(quantity=50))
        position = broker.list_positions()[0]

        self.assertEqual(position.quantity, -100)
        self.assertEqual(position.strike, 23000.0)
        self.assertEqual(position.option_type, OptionType.CALL)

    def test_position_going_flat_keeps_options_fields(self) -> None:
        broker = PaperBroker()

        broker.place_order(_naked_write_open_request(quantity=50))
        close_request = OrderRequest(
            symbol="NIFTY26AUG23000CE",
            exchange="NFO",
            side=OrderSide.BUY,
            quantity=50,
            order_type=OrderType.MARKET,
            product_type=ProductType.INTRADAY,
            price=100.0,
            strike=23000.0,
            expiry="2026-08-27",
            option_type=OptionType.CALL,
            lot_size=50,
            position_effect=PositionEffect.CLOSE,
        )
        broker.place_order(close_request)
        position = broker.list_positions()[0]

        self.assertEqual(position.quantity, 0)
        self.assertEqual(position.strike, 23000.0)
        self.assertEqual(position.option_type, OptionType.CALL)

    def test_opposite_direction_reducing_position_keeps_options_fields(self) -> None:
        broker = PaperBroker()

        broker.place_order(_naked_write_open_request(quantity=50))
        partial_close_request = OrderRequest(
            symbol="NIFTY26AUG23000CE",
            exchange="NFO",
            side=OrderSide.BUY,
            quantity=20,
            order_type=OrderType.MARKET,
            product_type=ProductType.INTRADAY,
            price=100.0,
            strike=23000.0,
            expiry="2026-08-27",
            option_type=OptionType.CALL,
            lot_size=20,
            position_effect=PositionEffect.CLOSE,
        )
        broker.place_order(partial_close_request)
        position = broker.list_positions()[0]

        self.assertEqual(position.quantity, -30)
        self.assertEqual(position.strike, 23000.0)
        self.assertEqual(position.option_type, OptionType.CALL)


if __name__ == "__main__":
    unittest.main()
