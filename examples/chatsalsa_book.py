"""Ejemplo: cómo ChatSalsa pide un libro impreso al servicio.

Flujo: el PDF del libro ya está generado y subido a una URL pública
(S3, Supabase Storage, R2…). Gelato se lo descarga de ahí.

1. Presupuesto → el usuario elige envío y paga en ChatSalsa (Stripe).
2. Pedido en Gelato con shippingMethod del presupuesto.
3. Consultar estado o recibir cambios por webhook.
"""
import os
import sys
import urllib.request
import json

BASE = os.environ.get("PRINT_SERVICE_URL", "http://localhost:8000")
KEY = os.environ.get("SERVICE_API_KEY", "dev-key")
BOOK_UID = ("photobooks-hardcover_pf_210x210-mm_pt_170-gsm-coated-silk_cl_4-4_ccl_4-4_bt_glued-left_"
            "ct_matt-lamination_prt_1-0_ver")  # confirmar en el catálogo de Gelato


def call(method, path, body=None):
    req = urllib.request.Request(f"{BASE}{path}", method=method,
                                 data=json.dumps(body).encode() if body else None,
                                 headers={"X-Service-Key": KEY, "Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


shipping = {"firstName": "Lucía", "lastName": "García", "addressLine1": "C/ Mayor 12, 3º B",
            "city": "Madrid", "postCode": "28013", "country": "ES", "email": "lucia@example.com"}
item = {"ref": "libro", "productUid": BOOK_UID, "pageCount": 52,
        "fileUrl": "https://files.chatsalsa.app/books/abc123.pdf"}

# 1) presupuesto
q = call("POST", "/quotes", {"app": "chatsalsa", "items": [item], "shipping": shipping})
opts = q["quotes"][0]["shippingOptions"]
print("Opciones de envío:")
for o in opts:
    print(f"  {o['uid']:<20} {o['price']:>6.2f} € · {o['minDays']}-{o['maxDays']} días · total {o['total']:.2f} €")
chosen = opts[0]["uid"]

# 2) pedido (orderRef = id del pedido en ChatSalsa; repetirlo no crea otro pedido)
order = call("POST", "/orders", {"app": "chatsalsa", "orderRef": "book_abc123", "customerRef": "user_42",
                                 "items": [item], "shipping": shipping, "shippingMethod": chosen,
                                 "draft": "--draft" in sys.argv,
                                 "metadata": {"chat": "abc123", "pages": 52}})
print("Pedido:", order["id"], "estado:", order["status"], "gelato:", order["gelato_order_id"])

# 3) estado
o = call("GET", f"/orders/{order['id']}?refresh=true")
print("Estado actual:", o["status"], "| tracking:", o.get("tracking"))
