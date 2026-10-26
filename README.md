# print-service

Servicio común de **impresión bajo demanda y envío** para los proyectos de Comartinvi
(ChatSalsa libros, tienda de cuadros, cuadernos, baraja…). Habla con Gelato; las apps
hablan con este servicio en términos sencillos (producto, archivo, dirección) y no
necesitan saber nada de Gelato.

```
ChatSalsa ──┐
Tienda ─────┼──▶ print-service ──▶ Gelato (impresión + envío) ──▶ webhooks ──▶ print-service ──▶ apps
Otros ──────┘        │
                 Postgres (Supabase) · pedidos + eventos
```

## Qué hace

| Endpoint | Para qué |
|---|---|
| `POST /quotes` | Precio del producto + opciones de envío (precio, plazos) para una dirección |
| `POST /orders` | Crea el pedido en Gelato. `draft: true` lo deja en borrador (no se imprime) |
| `POST /orders/{id}/submit` | Convierte un borrador en pedido real |
| `POST /orders/{id}/cancel` | Cancela (o borra el borrador). Gelato no deja cancelar una vez impreso |
| `GET /orders/{id}?refresh=true` | Estado simplificado + tracking; con `refresh` consulta a Gelato |
| `GET /orders/{id}/events` | Historial: creación, webhooks recibidos, cambios |
| `GET /orders?app=…&status=…` | Listado |
| `POST /files/validate` | Comprueba un PDF/imagen contra el tamaño del producto (proporción, dpi, páginas, fuentes) |
| `GET /products/{uid}` · `/prices` · `/cover-dimensions` | Catálogo de Gelato (passthrough) |
| `GET /shipping/methods?country=ES` | Métodos de envío disponibles |
| `POST /webhooks/gelato/{secret}` | Recibe los eventos de Gelato y actualiza estado y tracking |

Estados que devuelve a las apps: `draft · received · accepted · on_hold · in_production ·
shipped · delivered · failed · canceled · returned`.

Reglas que ya aplica: pedidos idempotentes por `(app, orderRef)`; límites de longitud de
dirección de Gelato (35 caracteres por línea, etc.); `state` obligatorio en US/CA/AU;
pedidos partidos por Gelato en varios envíos (mismo `orderReferenceId`) se reconcilian.

## Arrancar en local

```bash
pip install -r requirements.txt
cp .env.example .env            # sin GELATO_API_KEY arranca en modo mock
uvicorn app.main:app --reload
python examples/chatsalsa_book.py --draft
pytest
```

En modo mock no sale nada a Gelato; los precios y plazos son inventados pero el circuito
completo (presupuesto → pedido → webhook → tracking) funciona igual.

## Desplegar en Render

1. Crear el servicio desde este repo (`render.yaml` ya lo define).
2. En el dashboard de Render, variables: `GELATO_API_KEY` (Gelato → Developer → API keys),
   `DATABASE_URL` (cadena Postgres de Supabase) y `RETURN_EMAIL`.
3. En Gelato → API Portal → Webhooks, registrar
   `https://<servicio>.onrender.com/webhooks/gelato/<WEBHOOK_SECRET>`.
4. Primer pedido real con `draft: true`; revisarlo en el dashboard de Gelato; `submit`.

## Cómo lo usa cada app

**ChatSalsa (libro)**: genera el PDF (portada + interior + contraportada, páginas pares),
lo sube a una URL pública temporal, llama a `/quotes` para mostrar precio y envíos, cobra
con Stripe, y entonces `/orders` con `shippingMethod` del presupuesto. Borra el PDF cuando
el pedido pase a `shipped`. Ver `examples/chatsalsa_book.py`.

**Tienda de cuadros (Shopify)**: no necesita este servicio para vender; la app de Gelato
para Shopify ya hace el circuito. Lo usa para `/files/validate` antes de subir diseños
(`examples/validate_file.sh diseño.png canvas_50x70`) y, si algún día se vende fuera de
Shopify, para pedir.

## Especificaciones de archivo (orientativas)

| spec | tamaño final | sangrado | dpi mínimo |
|---|---|---|---|
| `canvas_50x70` | 500×700 mm | 38 mm (borde del bastidor) | 120 |
| `canvas_30x40` | 300×400 mm | 38 mm | 150 |
| `poster_50x70` | 500×700 mm | 3 mm | 150 |
| `book_a5` | 148×210 mm | 3 mm | 200 |

Las definitivas se confirman con el producto en Gelato (`/products/{uid}`) y, para libros,
con `/cover-dimensions?pageCount=N` (ancho de lomo). Pide muestras antes de vender.

## Estructura

```
app/
  config.py    variables de entorno
  gelato.py    cliente Gelato (sin dependencias) + MockGelato
  db.py        pedidos y eventos (SQLite o Postgres)
  service.py   lógica: presupuestos, pedidos, estados, webhooks
  files.py     validación de archivos de impresión
  main.py      API HTTP (FastAPI)
tests/         12 tests sobre el núcleo (no necesitan FastAPI ni red)
examples/      llamadas de ejemplo
```
