import unittest

from core.broker.base import (
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    ProductType,
)
from core.broker.kite_adapter import KiteBrokerAdapter
from core.broker.kite_errors import KiteTransportError
from core.broker.kite_models import KiteCredentials


class KiteBrokerAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        credentials = KiteCredentials(api_key="test_api_key", access_token="test_access_token")
        self.adapter = KiteBrokerAdapter(credentials=credentials, transport=object())  # type: ignore[arg-type]

    def test_build_kite_order_request_maps_fields_correctly(self) -> None:
        order = OrderRequest(
            symbol="RELIANCE",
            exchange="NSE",
            side=OrderSide.BUY,
            quantity=5,
            order_type=OrderType.MARKET,
            product_type=ProductType.INTRADAY,
        )

        kite_request = self.adapter._build_kite_order_request(order)

        self.assertEqual(kite_request.exchange, "NSE")
        self.assertEqual(kite_request.symbol, "RELIANCE")
        self.assertEqual(kite_request.side, "BUY")
        self.assertEqual(kite_request.quantity, 5)
        self.assertEqual(kite_request.order_type, "MARKET")
        self.assertEqual(kite_request.product_type, "MIS")  # INTRADAY -> Kite's real code
        self.assertEqual(kite_request.validity, "DAY")
        self.assertEqual(kite_request.variety, "regular")

    def test_build_kite_order_request_maps_delivery_and_stop_order_type(self) -> None:
        order = OrderRequest(
            symbol="TCS",
            exchange="NSE",
            side=OrderSide.SELL,
            quantity=3,
            order_type=OrderType.STOP,
            product_type=ProductType.DELIVERY,
            trigger_price=3700.0,
        )

        kite_request = self.adapter._build_kite_order_request(order)

        self.assertEqual(kite_request.product_type, "CNC")  # DELIVERY -> Kite's real code
        self.assertEqual(kite_request.order_type, "SL")  # STOP -> Kite's closest real code

    def test_build_kite_order_request_carries_client_order_id_as_tag(self) -> None:
        order = OrderRequest(
            symbol="RELIANCE",
            exchange="NSE",
            side=OrderSide.BUY,
            quantity=5,
            order_type=OrderType.MARKET,
            product_type=ProductType.INTRADAY,
            client_order_id="idem-abc123",
        )

        kite_request = self.adapter._build_kite_order_request(order)

        self.assertEqual(kite_request.tag, "idem-abc123")

    def test_build_kite_order_request_leaves_tag_none_when_unset(self) -> None:
        order = OrderRequest(
            symbol="RELIANCE",
            exchange="NSE",
            side=OrderSide.BUY,
            quantity=5,
            order_type=OrderType.MARKET,
            product_type=ProductType.INTRADAY,
        )

        kite_request = self.adapter._build_kite_order_request(order)

        self.assertIsNone(kite_request.tag)

    def test_map_fetched_order_filled(self) -> None:
        from core.broker.kite_models import KiteOrderResponse

        response = KiteOrderResponse(
            order_id="KITE-123",
            status="COMPLETE",
            average_price=2501.0,
            filled_quantity=5,
            symbol="RELIANCE",
            exchange="NSE",
            side="BUY",
            order_type="MARKET",
            product_type="MIS",
            quantity=5,
            raw_response={"status": "COMPLETE"},
        )

        result = self.adapter._map_fetched_order(response)

        self.assertEqual(result.order_id, "KITE-123")
        self.assertEqual(result.status, OrderStatus.FILLED)
        self.assertEqual(result.average_price, 2501.0)
        self.assertEqual(result.filled_quantity, 5)

    def test_map_fetched_order_rejected(self) -> None:
        from core.broker.kite_models import KiteOrderResponse

        response = KiteOrderResponse(
            order_id="KITE-456",
            status="REJECTED",
            average_price=None,
            filled_quantity=0,
            symbol="RELIANCE",
            exchange="NSE",
            side="BUY",
            order_type="MARKET",
            product_type="MIS",
            quantity=5,
            raw_response={"status": "REJECTED"},
        )

        result = self.adapter._map_fetched_order(response)

        self.assertEqual(result.order_id, "KITE-456")
        self.assertEqual(result.status, OrderStatus.REJECTED)
        self.assertIsNone(result.average_price)
        self.assertEqual(result.filled_quantity, 0)

    def test_map_fetched_order_partial_fill_is_inferred(self) -> None:
        from core.broker.kite_models import KiteOrderResponse

        response = KiteOrderResponse(
            order_id="KITE-789",
            status="COMPLETE",
            average_price=2499.5,
            filled_quantity=3,
            symbol="TCS",
            exchange="NSE",
            side="SELL",
            order_type="LIMIT",
            product_type="CNC",
            quantity=10,
            raw_response={"status": "COMPLETE"},
        )

        result = self.adapter._map_fetched_order(response)

        self.assertEqual(result.status, OrderStatus.PARTIALLY_FILLED)
        self.assertEqual(result.side, OrderSide.SELL)
        self.assertEqual(result.order_type, OrderType.LIMIT)
        self.assertEqual(result.product_type, ProductType.DELIVERY)

    def test_place_order_places_then_fetches_confirmed_state(self) -> None:
        """
        Unlike Groww, Kite's placement response has no status at all - the
        adapter must follow it with a fetch. This is the core behavioral
        difference this test locks in.
        """
        order = OrderRequest(
            symbol="RELIANCE",
            exchange="NSE",
            side=OrderSide.BUY,
            quantity=5,
            order_type=OrderType.MARKET,
            product_type=ProductType.INTRADAY,
        )

        calls = []

        def fake_send_place_order(request):
            calls.append("place")
            from core.broker.kite_models import KiteOrderResponse

            return KiteOrderResponse(order_id="KITE-1", status="PUT ORDER REQ RECEIVED")

        def fake_fetch_order(order_id):
            calls.append("fetch")
            self.assertEqual(order_id, "KITE-1")
            from core.broker.kite_models import KiteOrderResponse

            return KiteOrderResponse(
                order_id="KITE-1",
                status="COMPLETE",
                average_price=2502.0,
                filled_quantity=5,
                symbol="RELIANCE",
                exchange="NSE",
                side="BUY",
                order_type="MARKET",
                product_type="MIS",
                quantity=5,
            )

        self.adapter._send_place_order = fake_send_place_order  # type: ignore[method-assign]
        self.adapter._fetch_order = fake_fetch_order  # type: ignore[method-assign]

        result = self.adapter.place_order(order)

        self.assertEqual(calls, ["place", "fetch"])
        self.assertEqual(result.order_id, "KITE-1")
        self.assertEqual(result.status, OrderStatus.FILLED)
        self.assertEqual(result.average_price, 2502.0)

    def test_cancel_order_success_path(self) -> None:
        from core.broker.kite_models import KiteCancelResponse

        def fake_send_cancel_order(order_id):
            self.assertEqual(order_id, "kite-cancel-1")
            return KiteCancelResponse(order_id="kite-cancel-1")

        self.adapter._send_cancel_order = fake_send_cancel_order  # type: ignore[method-assign]

        result = self.adapter.cancel_order("kite-cancel-1")

        self.assertTrue(result)

    def test_list_positions_maps_kite_positions_correctly(self) -> None:
        from core.broker.kite_models import KitePosition

        def fake_fetch_positions():
            return [
                KitePosition(
                    symbol="RELIANCE",
                    exchange="NSE",
                    quantity=10,
                    average_price=2500.0,
                    product_type="MIS",
                    last_price=2510.0,
                    unrealized_pnl=100.0,
                    realized_pnl=0.0,
                ),
                KitePosition(
                    symbol="TCS",
                    exchange="NSE",
                    quantity=-5,
                    average_price=3800.0,
                    product_type="CNC",
                    realized_pnl=50.0,
                ),
            ]

        self.adapter._fetch_positions = fake_fetch_positions  # type: ignore[method-assign]

        result = self.adapter.list_positions()

        self.assertEqual(len(result), 2)

        first, second = result
        self.assertEqual(first.symbol, "RELIANCE")
        self.assertEqual(first.product_type, ProductType.INTRADAY)
        self.assertEqual(first.last_price, 2510.0)
        self.assertEqual(first.unrealized_pnl, 100.0)

        self.assertEqual(second.symbol, "TCS")
        self.assertEqual(second.quantity, -5)
        self.assertEqual(second.product_type, ProductType.DELIVERY)
        self.assertEqual(second.realized_pnl, 50.0)

    def test_live_transport_implemented_flag_is_true(self) -> None:
        self.assertTrue(KiteBrokerAdapter.LIVE_TRANSPORT_IMPLEMENTED)


class FakeKiteTransport:
    """
    Test double for KiteHttpTransport: returns canned raw dicts instead of
    making real calls, so adapter <-> transport wiring can be exercised
    through the public Broker methods.
    """

    def __init__(self) -> None:
        self.place_order_calls: list[dict] = []
        self.cancel_order_calls: list[str] = []
        self.get_order_calls: list[str] = []
        self.list_positions_calls = 0
        self.place_order_response: dict = {"order_id": "KITE-X"}
        self.cancel_order_response: dict = {"order_id": "KITE-X"}
        self.get_order_response: dict = {}
        self.list_positions_response: dict = {"net": [], "day": []}

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


def make_adapter_with_fake_transport() -> tuple[KiteBrokerAdapter, FakeKiteTransport]:
    fake_transport = FakeKiteTransport()
    adapter = KiteBrokerAdapter(
        credentials=KiteCredentials(api_key="k", access_token="t"),
        mode="live",
        transport=fake_transport,  # type: ignore[arg-type]
    )
    return adapter, fake_transport


class KiteBrokerAdapterTransportWiringTests(unittest.TestCase):
    def test_place_order_calls_transport_place_then_get_and_maps_result(self) -> None:
        adapter, transport = make_adapter_with_fake_transport()
        transport.place_order_response = {"order_id": "KITE-1"}
        transport.get_order_response = {
            "order_id": "KITE-1",
            "status": "OPEN",
            "tradingsymbol": "RELIANCE",
            "exchange": "NSE",
            "transaction_type": "BUY",
            "order_type": "MARKET",
            "product": "MIS",
            "quantity": 5,
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
        self.assertEqual(sent_payload["tradingsymbol"], "RELIANCE")
        self.assertEqual(sent_payload["transaction_type"], "BUY")
        self.assertEqual(sent_payload["price"], 2500.0)
        self.assertEqual(sent_payload["validity"], "DAY")
        self.assertNotIn("tag", sent_payload)  # unset client_order_id -> no tag sent

        self.assertEqual(transport.get_order_calls, ["KITE-1"])
        self.assertEqual(result.order_id, "KITE-1")
        self.assertEqual(result.status, OrderStatus.OPEN)
        self.assertEqual(result.symbol, "RELIANCE")

    def test_place_order_forwards_client_order_id_as_tag(self) -> None:
        adapter, transport = make_adapter_with_fake_transport()
        transport.get_order_response = {
            "order_id": "KITE-1",
            "status": "OPEN",
            "tradingsymbol": "RELIANCE",
            "exchange": "NSE",
            "transaction_type": "BUY",
            "order_type": "MARKET",
            "product": "MIS",
            "quantity": 5,
            "filled_quantity": 0,
        }

        order_request = OrderRequest(
            symbol="RELIANCE",
            exchange="NSE",
            side=OrderSide.BUY,
            quantity=5,
            order_type=OrderType.MARKET,
            product_type=ProductType.INTRADAY,
            client_order_id="idem-xyz789",
        )

        adapter.place_order(order_request)

        sent_payload = transport.place_order_calls[0]
        self.assertEqual(sent_payload["tag"], "idem-xyz789")

    def test_cancel_order_calls_transport_and_reports_success(self) -> None:
        adapter, transport = make_adapter_with_fake_transport()
        transport.cancel_order_response = {"order_id": "KITE-2"}

        result = adapter.cancel_order("KITE-2")

        self.assertEqual(transport.cancel_order_calls, ["KITE-2"])
        self.assertTrue(result)

    def test_get_order_calls_transport_and_maps_full_order(self) -> None:
        adapter, transport = make_adapter_with_fake_transport()
        transport.get_order_response = {
            "order_id": "KITE-3",
            "status": "COMPLETE",
            "tradingsymbol": "TCS",
            "exchange": "NSE",
            "transaction_type": "SELL",
            "order_type": "LIMIT",
            "product": "CNC",
            "quantity": 10,
            "filled_quantity": 10,
            "average_price": 3800.0,
        }

        result = adapter.get_order("KITE-3")

        self.assertEqual(transport.get_order_calls, ["KITE-3"])
        self.assertEqual(result.symbol, "TCS")
        self.assertEqual(result.side, OrderSide.SELL)
        self.assertEqual(result.status, OrderStatus.FILLED)
        self.assertEqual(result.quantity, 10)
        self.assertEqual(result.average_price, 3800.0)

    def test_list_positions_calls_transport_and_maps_net_positions_only(self) -> None:
        adapter, transport = make_adapter_with_fake_transport()
        transport.list_positions_response = {
            "net": [
                {
                    "tradingsymbol": "RELIANCE",
                    "exchange": "NSE",
                    "quantity": 10,
                    "average_price": 2500.0,
                    "product": "MIS",
                    "last_price": 2510.0,
                    "unrealised": 100.0,
                    "realised": 0.0,
                }
            ],
            "day": [{"tradingsymbol": "SHOULD_NOT_APPEAR"}],
        }

        result = adapter.list_positions()

        self.assertEqual(transport.list_positions_calls, 1)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].symbol, "RELIANCE")
        self.assertEqual(result[0].product_type, ProductType.INTRADAY)

    def test_get_order_raises_on_malformed_transport_response(self) -> None:
        adapter, transport = make_adapter_with_fake_transport()
        transport.get_order_response = {"status": "OPEN"}  # missing order_id/tradingsymbol/etc

        with self.assertRaises(KiteTransportError):
            adapter.get_order("KITE-1")


if __name__ == "__main__":
    unittest.main()
