"""Configuración por variables de entorno. Ningún secreto va en el código."""
import os


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


GELATO_API_KEY = env("GELATO_API_KEY")
# Si no hay API key, el servicio arranca en modo mock: nada sale a Gelato.
GELATO_MOCK = env("GELATO_MOCK", "1" if not GELATO_API_KEY else "0") == "1"

# Clave que deben enviar las apps (ChatSalsa, tienda…) en la cabecera X-Service-Key.
SERVICE_API_KEY = env("SERVICE_API_KEY", "dev-key")

# Segmento secreto de la URL del webhook: /webhooks/gelato/<WEBHOOK_SECRET>
WEBHOOK_SECRET = env("WEBHOOK_SECRET", "dev-webhook")

# A quién avisar cuando un pedido cambia de estado (p. ej. el backend de ChatSalsa, que manda el email al cliente).
# Recibe POST JSON {orderReferenceId, status, tracking, reason} con la cabecera X-Service-Key = NOTIFY_KEY.
NOTIFY_URL = env("NOTIFY_URL")
NOTIFY_KEY = env("NOTIFY_KEY")

# Base de datos: postgres://… (Supabase/Render) o ruta de fichero SQLite.
DATABASE_URL = env("DATABASE_URL", "sqlite:///./print_service.db")

DEFAULT_CURRENCY = env("DEFAULT_CURRENCY", "EUR")

# Dirección de devoluciones por defecto (opcional)
RETURN_COMPANY = env("RETURN_COMPANY")
RETURN_EMAIL = env("RETURN_EMAIL")
