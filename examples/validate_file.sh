#!/usr/bin/env bash
# Valida un archivo de impresión contra una especificación conocida
# (canvas_50x70, canvas_30x40, canvas_70x100, poster_a4, poster_50x70, book_21x21)
# o contra un productUid de Gelato.
set -e
FILE="${1:?uso: validate_file.sh archivo spec|productUid}"
SPEC="${2:-canvas_50x70}"
BASE="${PRINT_SERVICE_URL:-http://localhost:8000}"
KEY="${SERVICE_API_KEY:-dev-key}"
if [[ "$SPEC" == *_* && "$SPEC" != canvas_* && "$SPEC" != poster_* && "$SPEC" != book_* ]]; then
  curl -s -H "X-Service-Key: $KEY" -F "file=@$FILE" -F "productUid=$SPEC" "$BASE/files/validate"
else
  curl -s -H "X-Service-Key: $KEY" -F "file=@$FILE" -F "spec=$SPEC" "$BASE/files/validate"
fi
echo
