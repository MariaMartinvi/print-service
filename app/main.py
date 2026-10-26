"""API HTTP del servicio de impresión.

Las apps (ChatSalsa, tienda, etc.) llaman a estos endpoints con la cabecera
X-Service-Key. Gelato llama al webhook con el secreto en la URL.
"""
from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import config
from .db import Store
from .files import KNOWN_SPECS, ProductSpec, spec_from_gelato_product, validate_bytes
from .gelato import GelatoClient, MockGelato
from .service import PrintService, ServiceError

app = FastAPI(title="print-service", version="0.1.0",
              description="Servicio común de impresión bajo demanda y envío (Gelato).")

_gelato = MockGelato() if config.GELATO_MOCK else GelatoClient(config.GELATO_API_KEY)
_store = Store(config.DATABASE_URL)
_return = {"companyName": config.RETURN_COMPANY, "email": config.RETURN_EMAIL} if config.RETURN_COMPANY else None
svc = PrintService(_gelato, _store, config.DEFAULT_CURRENCY, _return)


def auth(x_service_key: str = Header(default="")):
    if x_service_key != config.SERVICE_API_KEY:
        raise HTTPException(401, "X-Service-Key inválida")


@app.exception_handler(ServiceError)
async def _service_error(_: Request, e: ServiceError):
    return JSONResponse(status_code=e.status, content={"error": e.message})


# --- modelos de entrada ----------------------------------------------------------
class Item(BaseModel):
    ref: str | None = None
    productUid: str
    quantity: int = 1
    fileUrl: str | None = None
    files: list[dict] | None = None
    pageCount: int | None = None


class Shipping(BaseModel):
    firstName: str
    lastName: str
    addressLine1: str
    addressLine2: str | None = None
    city: str
    postCode: str
    state: str | None = None
    country: str
    email: str
    phone: str | None = None
    companyName: str | None = None


class QuoteIn(BaseModel):
    app: str
    items: list[Item]
    shipping: Shipping
    currency: str | None = None


class OrderIn(BaseModel):
    app: str = Field(description="Identificador de la app que pide: chatsalsa, tienda-arte, …")
    orderRef: str = Field(description="Id del pedido en la app. Idempotente: repetirlo devuelve el mismo pedido.")
    customerRef: str | None = None
    items: list[Item]
    shipping: Shipping
    shippingMethod: str | None = Field(default=None, description="uid de un presupuesto, o normal/express")
    draft: bool = Field(default=False, description="true = borrador en Gelato, no se imprime hasta /submit")
    currency: str | None = None
    metadata: dict[str, Any] | None = None


# --- endpoints ------------------------------------------------------------------------
@app.get("/health")
def health():
    return {"ok": True, "mock": config.GELATO_MOCK}


@app.get("/products/{product_uid}", dependencies=[Depends(auth)])
def product(product_uid: str):
    return svc.product(product_uid)


@app.get("/products/{product_uid}/cover-dimensions", dependencies=[Depends(auth)])
def cover_dimensions(product_uid: str, pageCount: int = Query(..., ge=2)):
    return svc.cover_dimensions(product_uid, pageCount)


@app.get("/products/{product_uid}/prices", dependencies=[Depends(auth)])
def prices(product_uid: str, country: str | None = None, currency: str | None = None,
           pageCount: int | None = None):
    return svc.prices(product_uid, country, currency, pageCount)


@app.get("/shipping/methods", dependencies=[Depends(auth)])
def shipping_methods(country: str | None = None):
    return svc.shipment_methods(country)


@app.post("/quotes", dependencies=[Depends(auth)])
def quote(q: QuoteIn):
    return svc.quote(q.app, [i.model_dump(exclude_none=True) for i in q.items],
                     q.shipping.model_dump(exclude_none=True), q.currency)


@app.post("/orders", dependencies=[Depends(auth)], status_code=201)
def create_order(o: OrderIn):
    return svc.create_order(o.app, o.orderRef, o.customerRef or "anon",
                            [i.model_dump(exclude_none=True) for i in o.items],
                            o.shipping.model_dump(exclude_none=True), o.shippingMethod, o.draft,
                            o.currency, o.metadata)


@app.get("/orders", dependencies=[Depends(auth)])
def list_orders(app_name: str | None = Query(default=None, alias="app"), status: str | None = None,
                limit: int = 100):
    return _store.list_orders(app_name, status, limit)


@app.get("/orders/{order_id}", dependencies=[Depends(auth)])
def get_order(order_id: str, refresh: bool = False):
    return svc.get_order(order_id, refresh)


@app.get("/orders/{order_id}/events", dependencies=[Depends(auth)])
def order_events(order_id: str):
    return svc.events(order_id)


@app.post("/orders/{order_id}/submit", dependencies=[Depends(auth)])
def submit_order(order_id: str):
    return svc.submit(order_id)


@app.post("/orders/{order_id}/cancel", dependencies=[Depends(auth)])
def cancel_order(order_id: str):
    return svc.cancel(order_id)


@app.post("/files/validate", dependencies=[Depends(auth)])
async def validate_file(file: UploadFile = File(...), spec: str = Form(default=""),
                        productUid: str = Form(default=""), widthMm: float = Form(default=0),
                        heightMm: float = Form(default=0), bleedMm: float = Form(default=3),
                        kind: str = Form(default="flat"), pages: int | None = Form(default=None)):
    """Valida un archivo contra un producto. `spec` = clave de KNOWN_SPECS,
    o bien productUid (lee dimensiones de Gelato), o bien widthMm/heightMm."""
    if spec:
        if spec not in KNOWN_SPECS:
            raise HTTPException(422, f"spec desconocida; válidas: {', '.join(KNOWN_SPECS)}")
        ps = KNOWN_SPECS[spec]
    elif widthMm and heightMm:
        ps = ProductSpec(productUid or "custom", widthMm, heightMm, bleed_mm=bleedMm, kind=kind, pages=pages)
    elif productUid:
        ps = spec_from_gelato_product(svc.product(productUid), productUid, bleed_mm=bleedMm, kind=kind)
        if not ps:
            raise HTTPException(422, "Gelato no publica dimensiones para ese producto; pasa widthMm/heightMm")
        ps.pages = pages
    else:
        raise HTTPException(422, "indica spec, productUid o widthMm+heightMm")
    data = await file.read()
    return validate_bytes(data, ps, file.filename or "").as_dict()


@app.post("/webhooks/gelato/{secret}")
async def gelato_webhook(secret: str, request: Request):
    if secret != config.WEBHOOK_SECRET:
        raise HTTPException(404)
    payload = await request.json()
    return svc.handle_webhook(payload)
