"""
Unit tests for core.broker.paper's client_order_id idempotency behavior.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_paper_broker.py -v
"""
from __future__ import annotations

import unittest

from core.broker.base import OrderRequest, OrderSide, OrderType, ProductType
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


if __name__ == "__main__":
    unittest.main()
