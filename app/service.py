"""Lógica del servicio: presupuestos, pedidos, estado y webhooks.

Independiente del framework web. Las apps hablan en términos propios
(app, order_ref, items, shipping) y este módulo traduce a Gelato.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from .db import Store
from .gelato import GelatoClient, GelatoError

# Estados de Gelato → estado simplificado para las apps
STATUS_MAP = {
    "draft": "draft",
    "created": "received", "uploading": "received", "passed": "accepted",
    "pending_approval": "on_hold", "on_hold": "on_hold", "not_connected": "on_hold",
    "pending_personalization": "on_hold", "digitizing": "on_hold",
    "in_production": "in_production", "printed": "in_production",
    "shipped": "shipped", "in_transit": "shipped",
    "delivered": "delivered",
    "failed": "failed", "canceled": "canceled", "returned": "returned",
}
TERMINAL = {"delivered", "failed", "canceled", "returned"}


class ServiceError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def simplify_status(gelato_status: str | None) -> str:
    return STATUS_MAP.get(gelato_status or "", gelato_status or "unknown")


def _address(shipping: dict) -> dict:
    """Valida y normaliza la dirección al formato Gelato (límites de longitud incluidos)."""
    required = ["firstName", "lastName", "addressLine1", "city", "postCode", "country", "email"]
    missing = [k for k in required if not shipping.get(k)]
    if missing:
        raise ServiceError(422, f"faltan campos de dirección: {', '.join(missing)}")
    if shipping["country"] in ("US", "CA", "AU") and not shipping.get("state"):
        raise ServiceError(422, "state es obligatorio para US, CA y AU")
    limits = {"firstName": 25, "lastName": 25, "companyName": 60, "addressLine1": 35,
              "addressLine2": 35, "city": 30, "postCode": 15, "state": 35, "phone": 25}
    out = {}
    for k, v in shipping.items():
        if v in (None, ""):
            continue
        if k in limits and len(str(v)) > limits[k]:
            raise ServiceError(422, f"{k} supera {limits[k]} caracteres")
        out[k] = v
    out["country"] = str(out["country"]).upper()
    return out


def _items(items: list[dict]) -> list[dict]:
    if not items:
        raise ServiceError(422, "el pedido no tiene items")
    out, seen = [], set()
    for i, it in enumerate(items):
        ref = str(it.get("ref") or it.get("itemReferenceId") or f"item-{i + 1}")
        if ref in seen:
            raise ServiceError(422, f"ref de item repetida: {ref}")
        seen.add(ref)
        if not it.get("productUid"):
            raise ServiceError(422, f"item {ref}: falta productUid")
        files = it.get("files")
        if not files and it.get("fileUrl"):
            files = [{"type": "default", "url": it["fileUrl"]}]
        if not files:
            raise ServiceError(422, f"item {ref}: falta fileUrl o files")
        g = {"itemReferenceId": ref, "productUid": it["productUid"],
             "quantity": int(it.get("quantity", 1)), "files": files}
        if it.get("pageCount"):
            g["pageCount"] = int(it["pageCount"])
        out.append(g)
    return out


class PrintService:
    def __init__(self, gelato: GelatoClient, store: Store, default_currency: str = "EUR",
                 return_address: dict | None = None):
        self.gelato = gelato
        self.store = store
        self.currency = default_currency
        self.return_address = return_address or None

    # --- catálogo (passthrough) -----------------------------------------------
    def product(self, product_uid: str) -> dict:
        return self._g(self.gelato.get_product, product_uid)

    def cover_dimensions(self, product_uid: str, page_count: int) -> dict:
        return self._g(self.gelato.cover_dimensions, product_uid, page_count)

    def prices(self, product_uid: str, country: str | None, currency: str | None, page_count: int | None) -> list:
        return self._g(self.gelato.product_prices, product_uid, country, currency or self.currency, page_count)

    def shipment_methods(self, country: str | None) -> dict:
        return self._g(self.gelato.shipment_methods, country)

    # --- presupuesto ---------------------------------------------------------------
    def quote(self, app: str, items: list[dict], shipping: dict, currency: str | None = None) -> dict:
        payload = {
            "orderReferenceId": f"quote-{uuid.uuid4().hex[:12]}",
            "customerReferenceId": f"{app}-quote",
            "currency": currency or self.currency,
            "allowMultipleQuotes": True,
            "recipient": _address(shipping),
            "products": _items(items),
        }
        raw = self._g(self.gelato.quote, payload)
        quotes = []
        for q in raw.get("quotes", []):
            products_total = round(sum((p.get("price") or 0) for p in q.get("products") or []), 2)
            quotes.append({
                "quoteId": q.get("id"), "fulfillmentCountry": q.get("fulfillmentCountry"),
                "itemRefs": q.get("itemReferenceIds", []),
                "productsTotal": products_total,
                "currency": payload["currency"],
                "shippingOptions": [
                    {"uid": m.get("shipmentMethodUid"), "name": m.get("name"), "type": m.get("type"),
                     "price": m.get("price", 0), "minDays": m.get("minDeliveryDays"), "maxDays": m.get("maxDeliveryDays"),
                     "minDate": m.get("minDeliveryDate"), "maxDate": m.get("maxDeliveryDate"),
                     "total": round(products_total + (m.get("price") or 0), 2)}
                    for m in sorted(q.get("shipmentMethods") or [], key=lambda m: m.get("price") or 0)
                ],
            })
        return {"currency": payload["currency"], "quotes": quotes}

    # --- pedidos ---------------------------------------------------------------------
    def create_order(self, app: str, order_ref: str, customer_ref: str, items: list[dict],
                     shipping: dict, shipping_method: str | None = None, draft: bool = False,
                     currency: str | None = None, metadata: dict | None = None) -> dict:
        if not app or not order_ref:
            raise ServiceError(422, "app y order_ref son obligatorios")
        existing = self.store.get_order_by_ref(app, order_ref)
        if existing and existing.get("status") != "failed":
            return existing  # idempotente: mismo pedido, misma respuesta (un intento fallido se reintenta)
        g_items = _items(items)
        g_ship = _address(shipping)
        meta = dict(metadata or {})
        meta.setdefault("app", app)
        payload = {
            "orderType": "draft" if draft else "order",
            "orderReferenceId": f"{app}:{order_ref}",
            "customerReferenceId": f"{app}:{customer_ref or 'anon'}",
            "currency": currency or self.currency,
            "items": g_items,
            "shippingAddress": g_ship,
            "metadata": [{"key": str(k)[:100], "value": str(v)[:100]} for k, v in list(meta.items())[:20]],
        }
        if shipping_method:
            payload["shipmentMethodUid"] = shipping_method
        if self.return_address:
            payload["returnAddress"] = self.return_address
        local_id = str(uuid.uuid4())
        self.store.insert_order({
            "id": local_id, "app": app, "order_ref": order_ref, "customer_ref": customer_ref or "anon",
            "order_type": payload["orderType"], "status": "pending_submit", "currency": payload["currency"],
            "items": g_items, "shipping": g_ship, "metadata": meta,
        })
        try:
            raw = self._g(self.gelato.create_order, payload)
        except ServiceError as e:
            self.store.update_order(local_id, status="failed")
            self.store.add_event("service", "gelato_create_failed", {"error": e.message}, order_id=local_id)
            raise
        order = self._sync_from_gelato(local_id, raw)
        self.store.add_event("service", "order_created", {"gelato": raw}, order_id=local_id,
                             gelato_order_id=raw.get("id"))
        return order

    def get_order(self, order_id: str, refresh: bool = False) -> dict:
        o = self.store.get_order(order_id)
        if not o:
            raise ServiceError(404, "pedido no encontrado")
        if refresh and o.get("gelato_order_id") and o["status"] not in TERMINAL:
            raw = self._g(self.gelato.get_order, o["gelato_order_id"])
            o = self._sync_from_gelato(order_id, raw)
        return o

    def submit(self, order_id: str) -> dict:
        o = self.get_order(order_id)
        if o["order_type"] != "draft":
            raise ServiceError(409, "el pedido no es un borrador")
        raw = self._g(self.gelato.submit_draft, o["gelato_order_id"])
        self.store.add_event("service", "draft_submitted", {}, order_id=order_id, gelato_order_id=o["gelato_order_id"])
        return self._sync_from_gelato(order_id, raw)

    def cancel(self, order_id: str) -> dict:
        o = self.get_order(order_id)
        if o["status"] in TERMINAL:
            raise ServiceError(409, f"el pedido ya está en estado {o['status']}")
        if o["order_type"] == "draft":
            self._g(self.gelato.delete_draft, o["gelato_order_id"])
            self.store.add_event("service", "draft_deleted", {}, order_id=order_id)
            return self.store.update_order(order_id, status="canceled")
        raw = self._g(self.gelato.cancel_order, o["gelato_order_id"])
        self.store.add_event("service", "order_canceled", {}, order_id=order_id, gelato_order_id=o["gelato_order_id"])
        return self._sync_from_gelato(order_id, raw)

    def events(self, order_id: str) -> list[dict]:
        self.get_order(order_id)
        return self.store.list_events(order_id)

    # --- webhooks de Gelato ----------------------------------------------------------
    def handle_webhook(self, payload: dict) -> dict:
        """Procesa un evento de Gelato. Siempre lo guarda, aunque no reconozca el pedido."""
        event = payload.get("event", "unknown")
        gid = payload.get("orderId")
        ref = payload.get("orderReferenceId") or ""
        orders = self.store.get_orders_by_gelato_id(gid) if gid else []
        if not orders and ":" in ref:
            app, _, order_ref = ref.partition(":")
            o = self.store.get_order_by_ref(app, order_ref)
            orders = [o] if o else []
        for o in orders:
            self.store.add_event("gelato", event, payload, order_id=o["id"], gelato_order_id=gid)
        if not orders:
            self.store.add_event("gelato", event, payload, gelato_order_id=gid)
            return {"matched": 0}

        for o in orders:
            fields: dict[str, Any] = {}
            if event == "order_status_updated":
                fields["status"] = simplify_status(payload.get("fulfillmentStatus"))
                tracking = []
                for it in payload.get("items", []):
                    for f in it.get("fulfillments", []):
                        if f.get("trackingCode"):
                            tracking.append({"itemRef": it.get("itemReferenceId"), "code": f["trackingCode"],
                                             "url": f.get("trackingUrl"), "carrier": f.get("shipmentMethodName")})
                if tracking:
                    fields["tracking"] = _merge_tracking(o.get("tracking") or [], tracking)
            elif event == "order_item_tracking_code_updated" and payload.get("trackingCode"):
                fields["tracking"] = _merge_tracking(o.get("tracking") or [], [{
                    "itemRef": payload.get("itemReferenceId"), "code": payload["trackingCode"],
                    "url": payload.get("trackingUrl"), "carrier": payload.get("shipmentMethodName")}])
            elif event == "order_item_status_updated" and payload.get("status") == "failed":
                fields["status"] = "failed"
            if fields:
                self.store.update_order(o["id"], **fields)
        return {"matched": len(orders), "event": event}

    # --- internos -----------------------------------------------------------------------
    def _sync_from_gelato(self, local_id: str, raw: dict) -> dict:
        fields = {
            "gelato_order_id": raw.get("id"),
            "order_type": raw.get("orderType", "order"),
            "status": simplify_status(raw.get("fulfillmentStatus")),
        }
        sh = raw.get("shipment") or {}
        if sh:
            fields["shipment"] = {
                "methodUid": sh.get("shipmentMethodUid"), "methodName": sh.get("shipmentMethodName"),
                "minDays": sh.get("minDeliveryDays"), "maxDays": sh.get("maxDeliveryDays"),
                "minDate": sh.get("minDeliveryDate"), "maxDate": sh.get("maxDeliveryDate"),
                "fulfillmentCountry": sh.get("fulfillmentCountry"),
            }
            tracking = [{"code": p["trackingCode"], "url": p.get("trackingUrl")}
                        for p in sh.get("packages", []) if p.get("trackingCode")]
            if tracking:
                fields["tracking"] = tracking
        return self.store.update_order(local_id, **fields)

    def _g(self, fn, *args):
        try:
            return fn(*args)
        except GelatoError as e:
            detail = ""
            if isinstance(e.body, dict):
                errs = e.body.get("errors") or e.body.get("error") or e.body.get("details")
                if errs:
                    detail = " · " + json.dumps(errs, ensure_ascii=False)[:600]
            raise ServiceError(502 if e.status >= 500 or e.status == 0 else e.status, f"Gelato: {e.args[0]}{detail}")


def _merge_tracking(existing: list[dict], new: list[dict]) -> list[dict]:
    by_code = {t["code"]: t for t in existing}
    for t in new:
        by_code[t["code"]] = {**by_code.get(t["code"], {}), **t}
    return list(by_code.values())
