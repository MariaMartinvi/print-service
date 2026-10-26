"""Tests del núcleo (sin FastAPI): Gelato simulado + SQLite en memoria."""
import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import Store  # noqa: E402
from app.files import KNOWN_SPECS, ProductSpec, validate_bytes  # noqa: E402
from app.gelato import MockGelato  # noqa: E402
from app.service import PrintService, ServiceError, simplify_status  # noqa: E402

CANVAS = "canvas_500x700-mm_canvas_wood_4-0_hor"
BOOK = ("photobooks-hardcover_pf_210x210-mm_pt_170-gsm-coated-silk_cl_4-4_ccl_4-4_bt_glued-left_"
        "ct_matt-lamination_prt_1-0_ver")

SHIP = {"firstName": "Eva", "lastName": "Martín", "addressLine1": "Carrer de Martí 136",
        "city": "Barcelona", "postCode": "08024", "country": "es", "email": "eva@example.com"}


@pytest.fixture
def svc():
    return PrintService(MockGelato(), Store("sqlite:///:memory:"), "EUR")


def test_quote_returns_sorted_shipping_options(svc):
    q = svc.quote("chatsalsa", [{"productUid": BOOK, "pageCount": 52, "fileUrl": "https://x/book.pdf"}], SHIP)
    assert q["currency"] == "EUR"
    opts = q["quotes"][0]["shippingOptions"]
    assert [o["uid"] for o in opts] == ["mock_standard", "mock_express"]
    assert opts[0]["total"] == round(q["quotes"][0]["productsTotal"] + opts[0]["price"], 2)


def test_create_order_is_idempotent_and_persists(svc):
    o1 = svc.create_order("tienda-arte", "SHOP-1001", "cust-7",
                          [{"productUid": CANVAS, "fileUrl": "https://x/guadalupe.jpg"}], SHIP, "mock_standard")
    o2 = svc.create_order("tienda-arte", "SHOP-1001", "cust-7",
                          [{"productUid": CANVAS, "fileUrl": "https://x/guadalupe.jpg"}], SHIP, "mock_standard")
    assert o1["id"] == o2["id"]
    assert o1["status"] == "received"
    assert o1["gelato_order_id"]
    assert o1["shipping"]["country"] == "ES"          # normalizado a mayúsculas
    assert o1["items"][0]["itemReferenceId"] == "item-1"
    assert o1["shipment"]["methodUid"] == "mock_standard"
    assert len(svc.events(o1["id"])) == 1


def test_draft_then_submit_then_cancel(svc):
    o = svc.create_order("chatsalsa", "BK-1", "u1",
                         [{"productUid": BOOK, "pageCount": 52, "fileUrl": "https://x/b.pdf"}], SHIP, draft=True)
    assert o["status"] == "draft" and o["order_type"] == "draft"
    o = svc.submit(o["id"])
    assert o["status"] == "received" and o["order_type"] == "order"
    with pytest.raises(ServiceError) as e:
        svc.submit(o["id"])
    assert e.value.status == 409
    o = svc.cancel(o["id"])
    assert o["status"] == "canceled"
    with pytest.raises(ServiceError):
        svc.cancel(o["id"])


def test_cancel_draft_deletes_it(svc):
    o = svc.create_order("chatsalsa", "BK-2", "u1",
                         [{"productUid": BOOK, "pageCount": 52, "fileUrl": "https://x/b.pdf"}], SHIP, draft=True)
    o = svc.cancel(o["id"])
    assert o["status"] == "canceled"
    assert o["gelato_order_id"] not in svc.gelato.orders


def test_validation_errors(svc):
    with pytest.raises(ServiceError) as e:
        svc.create_order("app", "x", "c", [{"productUid": CANVAS}], SHIP)
    assert "fileUrl" in e.value.message
    with pytest.raises(ServiceError) as e:
        svc.create_order("app", "y", "c", [{"productUid": CANVAS, "fileUrl": "u"}], {**SHIP, "country": "US"})
    assert "state" in e.value.message
    with pytest.raises(ServiceError) as e:
        svc.create_order("app", "z", "c", [{"productUid": CANVAS, "fileUrl": "u"}],
                         {**SHIP, "addressLine1": "x" * 40})
    assert "35" in e.value.message
    with pytest.raises(ServiceError) as e:
        svc.create_order("app", "w", "c", [{"productUid": "nope", "fileUrl": "u"}], SHIP)
    assert e.value.status == 404
    assert svc.store.get_order_by_ref("app", "w")["status"] == "failed"


def test_webhook_updates_status_and_tracking(svc):
    o = svc.create_order("chatsalsa", "BK-3", "u1",
                         [{"ref": "libro", "productUid": BOOK, "pageCount": 52, "fileUrl": "https://x/b.pdf"}], SHIP)
    gid = o["gelato_order_id"]
    r = svc.handle_webhook({"id": "os_1", "event": "order_status_updated", "orderId": gid,
                            "orderReferenceId": "chatsalsa:BK-3", "fulfillmentStatus": "in_production",
                            "items": [{"itemReferenceId": "libro", "fulfillmentStatus": "in_production",
                                       "fulfillments": []}]})
    assert r["matched"] == 1
    assert svc.get_order(o["id"])["status"] == "in_production"
    svc.handle_webhook({"id": "tc_1", "event": "order_item_tracking_code_updated", "orderId": gid,
                        "orderReferenceId": "chatsalsa:BK-3", "itemReferenceId": "libro",
                        "trackingCode": "ABC123", "trackingUrl": "https://t/ABC123",
                        "shipmentMethodName": "Correos"})
    svc.handle_webhook({"id": "os_2", "event": "order_status_updated", "orderId": gid,
                        "orderReferenceId": "chatsalsa:BK-3", "fulfillmentStatus": "shipped",
                        "items": [{"itemReferenceId": "libro", "fulfillmentStatus": "shipped",
                                   "fulfillments": [{"trackingCode": "ABC123", "trackingUrl": "https://t/ABC123",
                                                     "shipmentMethodName": "Correos"}]}]})
    o = svc.get_order(o["id"])
    assert o["status"] == "shipped"
    assert o["tracking"] == [{"itemRef": "libro", "code": "ABC123", "url": "https://t/ABC123", "carrier": "Correos"}]
    assert [e["event"] for e in svc.events(o["id"])] == [
        "order_created", "order_status_updated", "order_item_tracking_code_updated", "order_status_updated"]


def test_webhook_unknown_order_is_stored_not_lost(svc):
    r = svc.handle_webhook({"event": "order_status_updated", "orderId": "nope", "orderReferenceId": "x:y",
                            "fulfillmentStatus": "shipped", "items": []})
    assert r == {"matched": 0}


def test_webhook_matches_split_orders_by_reference(svc):
    """Gelato puede partir un pedido en varios con el mismo orderReferenceId."""
    o = svc.create_order("tienda-arte", "SHOP-2", "c", [{"productUid": CANVAS, "fileUrl": "u"}], SHIP)
    r = svc.handle_webhook({"event": "order_status_updated", "orderId": "other-gelato-id",
                            "orderReferenceId": "tienda-arte:SHOP-2", "fulfillmentStatus": "passed", "items": []})
    assert r["matched"] == 1
    assert svc.get_order(o["id"])["status"] == "accepted"


def test_refresh_pulls_state_from_gelato(svc):
    o = svc.create_order("tienda-arte", "SHOP-3", "c", [{"productUid": CANVAS, "fileUrl": "u"}], SHIP)
    svc.gelato.advance(o["gelato_order_id"], "delivered", {"trackingCode": "T1", "trackingUrl": "https://t/T1"})
    o = svc.get_order(o["id"], refresh=True)
    assert o["status"] == "delivered"
    assert o["tracking"][0]["code"] == "T1"


def test_status_mapping():
    assert simplify_status("printed") == "in_production"
    assert simplify_status("in_transit") == "shipped"
    assert simplify_status("weird") == "weird"


# --- validación de archivos ---------------------------------------------------------
def _png(w, h):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 180, 150)).save(buf, "PNG")
    return buf.getvalue()


def test_image_validation_dpi_and_ratio():
    spec = KNOWN_SPECS["canvas_50x70"]  # 576x776 mm con bastidor
    need_w, need_h = spec.min_pixels()  # a 120 dpi
    ok = validate_bytes(_png(need_w, need_h), spec)
    assert ok.ok and ok.effective_dpi >= 120
    low = validate_bytes(_png(need_w // 2, need_h // 2), spec)
    assert not low.ok and "resolución" in low.errors[0]
    bad = validate_bytes(_png(1000, 1000), spec)
    assert not bad.ok and "proporción" in bad.errors[0]
    rot = validate_bytes(_png(need_h, need_w), spec)
    assert rot.ok and any("girada" in w for w in rot.warnings)


def test_pdf_validation():
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas
    spec = ProductSpec("book_test", 210, 210, bleed_mm=3, kind="book")
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(216 * mm, 216 * mm))
    for i in range(4):
        c.drawString(20 * mm, 20 * mm, f"página {i + 1}")
        c.showPage()
    c.save()
    r = validate_bytes(buf.getvalue(), spec)
    assert r.format == "pdf" and r.pages == 4 and r.ok, r.errors
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(216 * mm, 216 * mm))
    c.showPage(); c.showPage(); c.showPage()
    c.save()
    r = validate_bytes(buf.getvalue(), spec)
    assert not r.ok and "par" in r.errors[0]
    r = validate_bytes(b"hola", spec)
    assert not r.ok and "formato" in r.errors[0]
