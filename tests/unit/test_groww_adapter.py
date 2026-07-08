import unittest

from core.broker.base import (
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    ProductType,
)
from core.broker.groww_adapter import GrowwBrokerAdapter
from core.broker.groww_errors import GrowwTransportError
from core.broker.groww_models import (
    GrowwCancelResponse,
    GrowwCredentials,
    GrowwOrderResponse,
    GrowwPosition,
)


class GrowwBrokerAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        credentials = GrowwCredentials(
            api_key="test_api_key",
            access_token="test_access_token",
        )
        self.adapter = GrowwBrokerAdapter(credentials=credentials)

    def test_build_groww_order_request_maps_fields_correctly(self) -> None:
        order = OrderRequest(
            symbol="RELIANCE",
            exchange="NSE",
            side=OrderSide.BUY,
            quantity=5,
            order_type=OrderType.MARKET,
            product_type=ProductType.INTRADAY,
        )

        groww_request = self.adapter._build_groww_order_request(order)

        self.assertEqual(groww_request.exchange, "NSE")
        self.assertEqual(groww_request.symbol, "RELIANCE")
        self.assertEqual(groww_request.side, "BUY")
        self.assertEqual(groww_request.quantity, 5)
        self.assertEqual(groww_request.order_type, "MARKET")
        self.assertEqual(groww_request.product_type, "INTRADAY")

    def test_map_order_response_filled(self) -> None:
        order = OrderRequest(
            symbol="RELIANCE",
            exchange="NSE",
            side=OrderSide.BUY,
            quantity=5,
            order_type=OrderType.MARKET,
            product_type=ProductType.INTRADAY,
        )

        response = GrowwOrderResponse(
            order_id="groww-123",
            status="FILLED",
            average_price=2501.0,
            filled_quantity=5,
            raw_response={"status": "FILLED"},
        )

        result = self.adapter._map_order_response(order, response)

        self.assertEqual(result.order_id, "groww-123")
        self.assertEqual(result.status, OrderStatus.FILLED)
        self.assertEqual(result.average_price, 2501.0)
        self.assertEqual(result.filled_quantity, 5)

    def test_map_order_response_rejected(self) -> None:
        order = OrderRequest(
            symbol="RELIANCE",
            exchange="NSE",
            side=OrderSide.BUY,
            quantity=5,
            order_type=OrderType.MARKET,
            product_type=ProductType.INTRADAY,
        )

        response = GrowwOrderResponse(
            order_id="groww-456",
            status="REJECTED",
            average_price=None,
            filled_quantity=0,
            raw_response={"status": "REJECTED"},
        )

        result = self.adapter._map_order_response(order, response)

        self.assertEqual(result.order_id, "groww-456")
        self.assertEqual(result.status, OrderStatus.REJECTED)
        self.assertIsNone(result.average_price)
        self.assertEqual(result.filled_quantity, 0)

    def test_place_order_calls_builder_sender_and_mapper(self) -> None:
        order = OrderRequest(
            symbol="RELIANCE",
            exchange="NSE",
            side=OrderSide.BUY,
            quantity=5,
            order_type=OrderType.MARKET,
            product_type=ProductType.INTRADAY,
        )

        def fake_send_place_order(request):
            self.assertEqual(request.symbol, "RELIANCE")
            return GrowwOrderResponse(
                order_id="groww-789",
                status="FILLED",
                average_price=2502.0,
                filled_quantity=5,
                raw_response={"status": "FILLED"},
            )

        self.adapter._send_place_order = fake_send_place_order  # type: ignore[method-assign]

        result = self.adapter.place_order(order)

        self.assertEqual(result.order_id, "groww-789")
        self.assertEqual(result.status, OrderStatus.FILLED)
        self.assertEqual(result.average_price, 2502.0)
        self.assertEqual(result.filled_quantity, 5)

    def test_cancel_order_success_path(self) -> None:
        def fake_send_cancel_order(order_id):
            self.assertEqual(order_id, "groww-cancel-1")
            return GrowwCancelResponse(
                order_id="groww-cancel-1",
                status="CANCELLED",
                raw_response={"status": "CANCELLED"},
            )

        self.adapter._send_cancel_order = fake_send_cancel_order  # type: ignore[method-assign]

        result = self.adapter.cancel_order("groww-cancel-1")

        self.assertTrue(result)

    def test_cancel_order_failure_path(self) -> None:
        def fake_send_cancel_order(order_id):
            return GrowwCancelResponse(
                order_id="groww-cancel-2",
                status="REJECTED",
                raw_response={"status": "REJECTED"},
            )

        self.adapter._send_cancel_order = fake_send_cancel_order  # type: ignore[method-assign]

        result = self.adapter.cancel_order("groww-cancel-2")

        self.assertFalse(result)

    def test_get_order_maps_fetched_order_correctly(self) -> None:
        def fake_fetch_order(order_id):
            self.assertEqual(order_id, "groww-fetch-1")
            return GrowwOrderResponse(
                order_id="groww-fetch-1",
                status="PARTIALLY_FILLED",
                average_price=2499.5,
                filled_quantity=3,
                symbol="TCS",
                exchange="NSE",
                side="SELL",
                order_type="LIMIT",
                product_type="DELIVERY",
                quantity=10,
                raw_response={"status": "PARTIALLY_FILLED"},
            )

        self.adapter._fetch_order = fake_fetch_order  # type: ignore[method-assign]

        result = self.adapter.get_order("groww-fetch-1")

        self.assertEqual(result.order_id, "groww-fetch-1")
        self.assertEqual(result.symbol, "TCS")
        self.assertEqual(result.exchange, "NSE")
        self.assertEqual(result.side, OrderSide.SELL)
        self.assertEqual(result.order_type, OrderType.LIMIT)
        self.assertEqual(result.product_type, ProductType.DELIVERY)
        self.assertEqual(result.quantity, 10)
        self.assertEqual(result.filled_quantity, 3)
        self.assertEqual(result.status, OrderStatus.PARTIALLY_FILLED)
        self.assertEqual(result.average_price, 2499.5)

    def test_list_positions_maps_groww_positions_correctly(self) -> None:
        def fake_fetch_positions():
            return [
                GrowwPosition(
                    symbol="RELIANCE",
                    exchange="NSE",
                    quantity=10,
                    average_price=2500.0,
                    product_type="INTRADAY",
                    last_price=2510.0,
                    unrealized_pnl=100.0,
                    realized_pnl=0.0,
                ),
                GrowwPosition(
                    symbol="TCS",
                    exchange="NSE",
                    quantity=-5,
                    average_price=3800.0,
                    product_type="DELIVERY",
                    last_price=3780.0,
                    unrealized_pnl=100.0,
                    realized_pnl=50.0,
                ),
            ]

        self.adapter._fetch_positions = fake_fetch_positions  # type: ignore[method-assign]

        result = self.adapter.list_positions()

        self.assertEqual(len(result), 2)

        first, second = result
        self.assertEqual(first.symbol, "RELIANCE")
        self.assertEqual(first.exchange, "NSE")
        self.assertEqual(first.quantity, 10)
        self.assertEqual(first.average_price, 2500.0)
        self.assertEqual(first.product_type, ProductType.INTRADAY)
        self.assertEqual(first.last_price, 2510.0)
        self.assertEqual(first.unrealized_pnl, 100.0)
        self.assertEqual(first.realized_pnl, 0.0)

        self.assertEqual(second.symbol, "TCS")
        self.assertEqual(second.quantity, -5)
        self.assertEqual(second.product_type, ProductType.DELIVERY)
        self.assertEqual(second.realized_pnl, 50.0)


class FakeGrowwTransport:
    """
    Test double for GrowwHttpTransport: returns canned raw JSON dicts
    instead of making real HTTP calls, so adapter <-> transport wiring
    can be exercised through the public Broker methods (not just the
    _send_*/_fetch_* seams).
    """

    def __init__(self) -> None:
        self.place_order_calls: list[dict] = []
        self.cancel_order_calls: list[str] = []
        self.get_order_calls: list[str] = []
        self.list_positions_calls = 0
        self.place_order_response: dict = {}
        self.cancel_order_response: dict = {}
        self.get_order_response: dict = {}
        self.list_positions_response: dict = {"positions": []}

    def place_order(self, payload: dict) -> dict:
        self.place_order_calls.append(payload)
        return self.place_order_response

    def cancel_order(self, order_id: str) -> dict:
        self.cancel_order_calls.append(order_id)
        return self.cancel_order_response

    def get_order(self, order_id: str) -> dict:
        self.get_order_calls.append(order_id)
        return self.get_order_response

    def list_positions(self) -> dict:
        self.list_positions_calls += 1
        return self.list_positions_response


def make_adapter_with_fake_transport() -> tuple[GrowwBrokerAdapter, FakeGrowwTransport]:
    fake_transport = FakeGrowwTransport()
    adapter = GrowwBrokerAdapter(
        credentials=GrowwCredentials(api_key="k", access_token="t"),
        mode="live",
        transport=fake_transport,
    )
    return adapter, fake_transport


class GrowwBrokerAdapterTransportWiringTests(unittest.TestCase):
    """
    End-to-end (through the public Broker methods) tests proving the
    adapter correctly calls the transport and parses its raw responses,
    without any real HTTP call.
    """

    def test_place_order_calls_transport_with_built_payload_and_maps_result(self) -> None:
        adapter, transport = make_adapter_with_fake_transport()
        transport.place_order_response = {
            "order_id": "GRW-1",
            "status": "NEW",
            "filled_quantity": 0,
        }

        order_request = OrderRequest(
            symbol="RELIANCE",
            exchange="NSE",
            side=OrderSide.BUY,
            quantity=5,
            order_type=OrderType.MARKET,
            product_type=ProductType.INTRADAY,
            price=2500.0,
        )

        result = adapter.place_order(order_request)

        self.assertEqual(len(transport.place_order_calls), 1)
        sent_payload = transport.place_order_calls[0]
        self.assertEqual(sent_payload["symbol"], "RELIANCE")
        self.assertEqual(sent_payload["side"], "BUY")
        self.assertEqual(sent_payload["price"], 2500.0)

        self.assertEqual(result.order_id, "GRW-1")
        self.assertEqual(result.status, OrderStatus.PENDING)
        self.assertEqual(result.symbol, "RELIANCE")

    def test_cancel_order_calls_transport_and_maps_result(self) -> None:
        adapter, transport = make_adapter_with_fake_transport()
        transport.cancel_order_response = {"order_id": "GRW-2", "status": "CANCELLED"}

        result = adapter.cancel_order("GRW-2")

        self.assertEqual(transport.cancel_order_calls, ["GRW-2"])
        self.assertTrue(result)

    def test_get_order_calls_transport_and_maps_full_order(self) -> None:
        adapter, transport = make_adapter_with_fake_transport()
        transport.get_order_response = {
            "order_id": "GRW-3",
            "status": "FILLED",
            "symbol": "TCS",
            "exchange": "NSE",
            "side": "SELL",
            "order_type": "LIMIT",
            "product_type": "DELIVERY",
            "quantity": 10,
            "filled_quantity": 10,
            "average_price": 3800.0,
        }

        result = adapter.get_order("GRW-3")

        self.assertEqual(transport.get_order_calls, ["GRW-3"])
        self.assertEqual(result.symbol, "TCS")
        self.assertEqual(result.side, OrderSide.SELL)
        self.assertEqual(result.status, OrderStatus.FILLED)
        self.assertEqual(result.quantity, 10)

    def test_list_positions_calls_transport_and_maps_all_positions(self) -> None:
        adapter, transport = make_adapter_with_fake_transport()
        transport.list_positions_response = {
            "positions": [
                {
                    "symbol": "RELIANCE",
                    "exchange": "NSE",
                    "quantity": 10,
                    "average_price": 2500.0,
                    "product_type": "INTRADAY",
                    "last_price": 2510.0,
                    "unrealized_pnl": 100.0,
                    "realized_pnl": 0.0,
                }
            ]
        }

        result = adapter.list_positions()

        self.assertEqual(transport.list_positions_calls, 1)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].symbol, "RELIANCE")
        self.assertEqual(result[0].product_type, ProductType.INTRADAY)

    def test_place_order_raises_on_malformed_transport_response(self) -> None:
        adapter, transport = make_adapter_with_fake_transport()
        transport.place_order_response = {"status": "NEW"}  # missing order_id

        order_request = OrderRequest(
            symbol="RELIANCE",
            exchange="NSE",
            side=OrderSide.BUY,
            quantity=5,
            order_type=OrderType.MARKET,
            product_type=ProductType.INTRADAY,
        )

        with self.assertRaises(GrowwTransportError):
            adapter.place_order(order_request)

    def test_live_transport_implemented_flag_is_true(self) -> None:
        self.assertTrue(GrowwBrokerAdapter.LIVE_TRANSPORT_IMPLEMENTED)


if __name__ == "__main__":
    unittest.main()
