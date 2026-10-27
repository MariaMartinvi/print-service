"""Cliente de la API de Gelato (v4 orders, v3 products, v1 shipment).

Solo usa la librería estándar (urllib), de modo que el núcleo del servicio
funciona y se prueba sin dependencias externas. Incluye un MockGelato con
el mismo contrato para desarrollo y tests.

Referencia: https://dashboard.gelato.com/docs/
"""
from __future__ import annotations

import json
import uuid
from datetime import date, timedelta
from typing import Any
from urllib import error, parse, request

ORDER_API = "https://order.gelatoapis.com"
PRODUCT_API = "https://product.gelatoapis.com"
SHIPMENT_API = "https://shipment.gelatoapis.com"


class GelatoError(Exception):
    def __init__(self, status: int, message: str, body: Any = None):
        super().__init__(f"Gelato {status}: {message}")
        self.status = status
        self.body = body


class GelatoClient:
    """Cliente real. Cada método devuelve el JSON de Gelato tal cual."""

    def __init__(self, api_key: str, timeout: float = 30.0):
        if not api_key:
            raise ValueError("GELATO_API_KEY vacía")
        self.api_key = api_key
        self.timeout = timeout

    # --- transporte -------------------------------------------------------
    def _request(self, method: str, url: str, payload: dict | None = None,
                 params: dict | None = None) -> Any:
        if params:
            url = f"{url}?{parse.urlencode({k: v for k, v in params.items() if v is not None})}"
        data = json.dumps(payload).encode() if payload is not None else None
        req = request.Request(url, data=data, method=method)
        req.add_header("X-API-KEY", self.api_key)
        req.add_header("Accept", "application/json")
        # Cloudflare (delante de gelatoapis.com) bloquea clientes sin User-Agent reconocible (error 1010)
        req.add_header("User-Agent", "print-service/0.1 (+https://chatsalsa.com; python-urllib)")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except error.HTTPError as e:
            raw = e.read()
            try:
                body = json.loads(raw)
            except Exception:
                body = raw.decode(errors="replace")
            msg = body.get("message") if isinstance(body, dict) else str(body)
            raise GelatoError(e.code, msg or e.reason, body) from None
        except error.URLError as e:
            raise GelatoError(0, f"sin conexión: {e.reason}") from None

    # --- catálogo ---------------------------------------------------------
    def list_catalogs(self) -> list[dict]:
        return self._request("GET", f"{PRODUCT_API}/v3/catalogs")

    def get_catalog(self, catalog_uid: str) -> dict:
        return self._request("GET", f"{PRODUCT_API}/v3/catalogs/{catalog_uid}")

    def search_products(self, catalog_uid: str, attribute_filters: dict | None = None,
                        limit: int = 50, offset: int = 0) -> dict:
        payload = {"attributeFilters": attribute_filters or {}, "limit": limit, "offset": offset}
        return self._request("POST", f"{PRODUCT_API}/v3/catalogs/{catalog_uid}/products:search", payload)

    def get_product(self, product_uid: str) -> dict:
        return self._request("GET", f"{PRODUCT_API}/v3/products/{product_uid}")

    def cover_dimensions(self, product_uid: str, page_count: int) -> dict:
        return self._request("GET", f"{PRODUCT_API}/v3/products/{product_uid}/cover-dimensions",
                             params={"pageCount": page_count})

    def product_prices(self, product_uid: str, country: str | None = None,
                       currency: str | None = None, page_count: int | None = None) -> list[dict]:
        return self._request("GET", f"{PRODUCT_API}/v3/products/{product_uid}/prices",
                             params={"country": country, "currency": currency, "pageCount": page_count})

    def stock_availability(self, product_uids: list[str]) -> dict:
        return self._request("POST", f"{PRODUCT_API}/v3/stock/region-availability",
                             {"products": product_uids})

    # --- envío --------------------------------------------------------------
    def shipment_methods(self, country: str | None = None) -> dict:
        return self._request("GET", f"{SHIPMENT_API}/v1/shipment-methods", params={"country": country})

    # --- pedidos ------------------------------------------------------------
    def quote(self, payload: dict) -> dict:
        return self._request("POST", f"{ORDER_API}/v4/orders:quote", payload)

    def create_order(self, payload: dict) -> dict:
        return self._request("POST", f"{ORDER_API}/v4/orders", payload)

    def get_order(self, order_id: str) -> dict:
        return self._request("GET", f"{ORDER_API}/v4/orders/{order_id}")

    def submit_draft(self, order_id: str) -> dict:
        """Convierte un borrador en pedido real (Order Patch API)."""
        return self._request("PATCH", f"{ORDER_API}/v4/orders/{order_id}", {"orderType": "order"})

    def cancel_order(self, order_id: str) -> dict:
        return self._request("POST", f"{ORDER_API}/v4/orders/{order_id}:cancel")

    def delete_draft(self, order_id: str) -> dict:
        return self._request("DELETE", f"{ORDER_API}/v4/orders/{order_id}")

    def search_orders(self, payload: dict) -> dict:
        return self._request("POST", f"{ORDER_API}/v4/orders:search", payload)


class MockGelato(GelatoClient):
    """Simula Gelato en memoria. Mismo contrato que GelatoClient.

    Precios y plazos son orientativos, no reales. Sirve para desarrollar y
    probar el circuito completo (presupuesto → pedido → webhook) sin API key.
    """

    PRODUCTS = {
        "canvas_500x700-mm_canvas_wood_4-0_hor": {"price": 24.90, "name": "Lienzo 50x70", "pages": False},
        "photobooks-hardcover_pf_210x210-mm_pt_170-gsm-coated-silk_cl_4-4_ccl_4-4_bt_glued-left_ct_matt-lamination_prt_1-0_ver":
            {"price": 18.00, "name": "Photo book tapa dura 21x21", "pages": True},
        "flat_product_pf_a4_pt_200-gsm-uncoated_cl_4-0_ct_none_prt_none_sft_none_set_none_hor":
            {"price": 3.50, "name": "Poster A4", "pages": False},
    }
    SHIPMENTS = [
        {"name": "Correos Standard", "shipmentMethodUid": "mock_standard", "price": 4.90,
         "minDeliveryDays": 4, "maxDeliveryDays": 7, "type": "normal"},
        {"name": "DHL Express", "shipmentMethodUid": "mock_express", "price": 14.90,
         "minDeliveryDays": 2, "maxDeliveryDays": 3, "type": "express"},
    ]

    def __init__(self):
        self.api_key = "mock"
        self.timeout = 0
        self.orders: dict[str, dict] = {}

    def _request(self, *a, **kw):  # pragma: no cover
        raise RuntimeError("MockGelato no hace peticiones")

    def list_catalogs(self):
        return [{"catalogUid": "wall-art", "title": "Wall art"},
                {"catalogUid": "photobooks-hardcover", "title": "Photo books hardcover"}]

    def get_product(self, product_uid):
        p = self.PRODUCTS.get(product_uid)
        if not p:
            raise GelatoError(404, "product not found")
        return {"productUid": product_uid, "title": p["name"], "isMultipage": p["pages"]}

    def cover_dimensions(self, product_uid, page_count):
        if not self.PRODUCTS.get(product_uid, {}).get("pages"):
            raise GelatoError(400, "not a multipage product")
        spine = round(0.11 * page_count + 6.0, 2)  # mm, aproximación
        return {"productUid": product_uid, "pagesCount": page_count, "measureUnit": "mm",
                "spineSize": {"width": spine, "height": 216}}

    def product_prices(self, product_uid, country=None, currency=None, page_count=None):
        p = self.PRODUCTS.get(product_uid)
        if not p:
            raise GelatoError(404, "product not found")
        base = p["price"] + (0.12 * page_count if p["pages"] and page_count else 0)
        return [{"productUid": product_uid, "country": country or "ES", "quantity": q,
                 "price": round(base * (1 - d), 2), "currency": currency or "EUR", "pageCount": page_count}
                for q, d in ((1, 0), (10, 0.1), (50, 0.2))]

    def shipment_methods(self, country=None):
        return {"shipmentMethods": self.SHIPMENTS}

    def quote(self, payload):
        products = []
        for p in payload["products"]:
            prices = self.product_prices(p["productUid"], payload["recipient"]["country"],
                                         payload.get("currency"), p.get("pageCount"))
            products.append({"itemReferenceId": p["itemReferenceId"], "productUid": p["productUid"],
                             "quantity": p["quantity"], "price": round(prices[0]["price"] * p["quantity"], 2),
                             "currency": payload.get("currency", "EUR")})
        today = date.today()
        methods = [dict(m, currency=payload.get("currency", "EUR"),
                        minDeliveryDate=str(today + timedelta(days=m["minDeliveryDays"])),
                        maxDeliveryDate=str(today + timedelta(days=m["maxDeliveryDays"])),
                        isPrivate=True, isBusiness=True, totalWeight=800, numberOfParcels=1)
                   for m in self.SHIPMENTS]
        return {"orderReferenceId": payload["orderReferenceId"],
                "quotes": [{"id": str(uuid.uuid4()), "itemReferenceIds": [p["itemReferenceId"] for p in products],
                            "fulfillmentCountry": "ES", "shipmentMethods": methods, "products": products}]}

    def create_order(self, payload):
        oid = str(uuid.uuid4())
        for it in payload["items"]:
            self.get_product(it["productUid"])
        status = "draft" if payload.get("orderType") == "draft" else "created"
        order = {"id": oid, "orderType": payload.get("orderType", "order"),
                 "orderReferenceId": payload["orderReferenceId"],
                 "customerReferenceId": payload["customerReferenceId"],
                 "fulfillmentStatus": status, "financialStatus": "pending",
                 "currency": payload.get("currency", "EUR"), "channel": "api",
                 "items": [dict(it, id=str(uuid.uuid4()), fulfillmentStatus=status) for it in payload["items"]],
                 "shippingAddress": payload["shippingAddress"],
                 "shipment": {"shipmentMethodUid": payload.get("shipmentMethodUid", "mock_standard"),
                              "shipmentMethodName": "Correos Standard", "minDeliveryDays": 4,
                              "maxDeliveryDays": 7, "fulfillmentCountry": "ES", "packages": []},
                 "metadata": payload.get("metadata", []), "connectedOrderIds": []}
        self.orders[oid] = order
        return order

    def get_order(self, order_id):
        if order_id not in self.orders:
            raise GelatoError(404, "order not found")
        return self.orders[order_id]

    def submit_draft(self, order_id):
        o = self.get_order(order_id)
        if o["orderType"] != "draft":
            raise GelatoError(400, "order is not a draft")
        o["orderType"] = "order"
        o["fulfillmentStatus"] = "created"
        for it in o["items"]:
            it["fulfillmentStatus"] = "created"
        return o

    def cancel_order(self, order_id):
        o = self.get_order(order_id)
        if o["fulfillmentStatus"] in ("printed", "shipped", "in_transit", "delivered"):
            raise GelatoError(409, "order already in production or shipped")
        o["fulfillmentStatus"] = "canceled"
        return o

    def delete_draft(self, order_id):
        o = self.get_order(order_id)
        if o["orderType"] != "draft":
            raise GelatoError(400, "only drafts can be deleted")
        del self.orders[order_id]
        return {}

    def search_orders(self, payload):
        return {"orders": list(self.orders.values())}

    # Utilidad para tests: simula un avance de estado como lo haría Gelato.
    def advance(self, order_id: str, status: str, tracking: dict | None = None) -> dict:
        o = self.get_order(order_id)
        o["fulfillmentStatus"] = status
        for it in o["items"]:
            it["fulfillmentStatus"] = status
        if tracking:
            o["shipment"]["packages"] = [dict(tracking, orderItemIds=[it["id"] for it in o["items"]])]
        return o
