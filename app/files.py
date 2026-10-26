"""Validación de archivos de impresión antes de enviarlos a Gelato.

Comprueba lo que suele devolver un pedido fallido o un producto mal impreso:
formato, número de páginas, proporción y resolución efectiva (dpi) para el
tamaño físico del producto. Gelato recomienda 300 dpi y PDF/X; 150 dpi es
el mínimo aceptable para lienzos grandes vistos a distancia.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field

MM_PER_INCH = 25.4
PT_PER_INCH = 72.0


@dataclass
class ProductSpec:
    """Especificación de impresión de un producto: tamaño final + sangrado."""
    product_uid: str
    width_mm: float
    height_mm: float
    bleed_mm: float = 3.0
    safe_mm: float = 5.0
    min_dpi: int = 150
    pages: int | None = None          # None = una sola cara; N = multipágina (incluye cubiertas)
    kind: str = "flat"                # flat | canvas | book

    @property
    def full_width_mm(self) -> float:
        return self.width_mm + 2 * self.bleed_mm

    @property
    def full_height_mm(self) -> float:
        return self.height_mm + 2 * self.bleed_mm

    def min_pixels(self, dpi: int | None = None) -> tuple[int, int]:
        d = dpi or self.min_dpi
        import math
        return (math.ceil(self.full_width_mm / MM_PER_INCH * d), math.ceil(self.full_height_mm / MM_PER_INCH * d))


# Especificaciones orientativas. Las definitivas se leen del producto en
# Gelato (get_product → dimensions) o de cover-dimensions para libros.
# Lienzos: el "sangrado" es el borde envolvente del bastidor (~38 mm);
# el motivo importante debe quedar dentro de la cara frontal.
KNOWN_SPECS: dict[str, ProductSpec] = {
    "canvas_50x70": ProductSpec("canvas_50x70", 500, 700, bleed_mm=38, safe_mm=15, min_dpi=120, kind="canvas"),
    "canvas_30x40": ProductSpec("canvas_30x40", 300, 400, bleed_mm=38, safe_mm=15, min_dpi=150, kind="canvas"),
    "canvas_70x100": ProductSpec("canvas_70x100", 700, 1000, bleed_mm=38, safe_mm=15, min_dpi=100, kind="canvas"),
    "poster_a4": ProductSpec("poster_a4", 210, 297, bleed_mm=3, safe_mm=5, min_dpi=200, kind="flat"),
    "poster_50x70": ProductSpec("poster_50x70", 500, 700, bleed_mm=3, safe_mm=5, min_dpi=150, kind="flat"),
    "book_a5": ProductSpec("book_a5", 148, 210, bleed_mm=3, safe_mm=10, min_dpi=200, kind="book"),
    "book_21x21": ProductSpec("book_21x21", 210, 210, bleed_mm=3, safe_mm=8, min_dpi=200, kind="book"),
}


@dataclass
class ValidationResult:
    ok: bool
    format: str
    pages: int
    width_mm: float | None = None
    height_mm: float | None = None
    effective_dpi: int | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def validate_bytes(data: bytes, spec: ProductSpec, filename: str = "") -> ValidationResult:
    head = data[:8]
    if head.startswith(b"%PDF"):
        return _validate_pdf(data, spec)
    if head.startswith(b"\x89PNG") or head[:3] == b"\xff\xd8\xff" or head[:4] in (b"II*\x00", b"MM\x00*"):
        return _validate_image(data, spec)
    r = ValidationResult(False, "unknown", 0)
    r.errors.append(f"formato no reconocido ({filename or 'sin nombre'}); se admiten PDF, PNG, JPEG y TIFF")
    return r


def _check_ratio(r: ValidationResult, spec: ProductSpec, w_mm: float, h_mm: float) -> None:
    want = spec.full_width_mm / spec.full_height_mm
    have = w_mm / h_mm
    # tolera orientación girada
    if abs(have - want) > 0.02 and abs((1 / have) - want) > 0.02:
        r.errors.append(f"proporción {have:.3f} no coincide con el producto {want:.3f} "
                        f"({spec.full_width_mm:.0f}x{spec.full_height_mm:.0f} mm con sangrado)")
    elif abs(have - want) > 0.02:
        r.warnings.append("la orientación del archivo está girada respecto al producto")
    # tamaño físico: el PDF debe medir exactamente el producto + sangrado
    if abs(w_mm - spec.full_width_mm) > 1 and abs(w_mm - spec.full_height_mm) > 1:
        r.warnings.append(f"el tamaño de página ({w_mm:.0f}x{h_mm:.0f} mm) no es el del producto con sangrado "
                          f"({spec.full_width_mm:.0f}x{spec.full_height_mm:.0f} mm); Gelato lo escalará")


def _validate_pdf(data: bytes, spec: ProductSpec) -> ValidationResult:
    from pypdf import PdfReader
    r = ValidationResult(True, "pdf", 0)
    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as e:
        r.ok = False
        r.errors.append(f"PDF ilegible: {e}")
        return r
    r.pages = len(reader.pages)
    if r.pages == 0:
        r.ok = False
        r.errors.append("el PDF no tiene páginas")
        return r
    if spec.pages is None and r.pages != 1 and spec.kind != "book":
        r.errors.append(f"el producto es de una cara y el PDF tiene {r.pages} páginas")
    if spec.pages and r.pages != spec.pages:
        r.errors.append(f"el producto espera {spec.pages} páginas (cubiertas incluidas) y el PDF tiene {r.pages}")
    if spec.kind == "book" and r.pages % 2:
        r.errors.append("un libro necesita un número par de páginas")
    page = reader.pages[0]
    box = page.mediabox
    w_mm = float(box.width) / PT_PER_INCH * MM_PER_INCH
    h_mm = float(box.height) / PT_PER_INCH * MM_PER_INCH
    r.width_mm, r.height_mm = round(w_mm, 1), round(h_mm, 1)
    _check_ratio(r, spec, w_mm, h_mm)
    # resolución de las imágenes incrustadas (la primera página basta como muestra)
    try:
        imgs = page.images
        if imgs:
            worst = None
            for im in imgs[:5]:
                px_w = im.image.width
                dpi = px_w / (w_mm / MM_PER_INCH)
                worst = dpi if worst is None else min(worst, dpi)
            if worst is not None:
                r.effective_dpi = int(worst)
                if worst < spec.min_dpi:
                    r.warnings.append(f"imágenes a ~{int(worst)} dpi (mínimo recomendado {spec.min_dpi})")
    except Exception:
        pass
    fonts_ok = True
    try:
        res = page.get("/Resources") or {}
        fonts = res.get("/Font") or {}
        for _, f in fonts.items():
            f = f.get_object()
            desc = f.get("/FontDescriptor")
            if desc is not None and not any(k in desc.get_object() for k in ("/FontFile", "/FontFile2", "/FontFile3")):
                fonts_ok = False
    except Exception:
        pass
    if not fonts_ok:
        r.errors.append("hay fuentes sin incrustar")
    r.ok = not r.errors
    return r


def _validate_image(data: bytes, spec: ProductSpec) -> ValidationResult:
    from PIL import Image
    r = ValidationResult(True, "image", 1)
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception as e:
        r.ok = False
        r.errors.append(f"imagen ilegible: {e}")
        return r
    r.format = im.format.lower() if im.format else "image"
    w, h = im.size
    if im.mode not in ("RGB", "CMYK", "L"):
        r.warnings.append(f"modo de color {im.mode}; conviene RGB sin transparencia")
    # Para una imagen, el tamaño físico lo fija el producto; calculamos dpi efectivos
    dpi_w = w / (spec.full_width_mm / MM_PER_INCH)
    dpi_h = h / (spec.full_height_mm / MM_PER_INCH)
    rotated = False
    if abs(w / h - spec.full_width_mm / spec.full_height_mm) > 0.02:
        if abs(h / w - spec.full_width_mm / spec.full_height_mm) <= 0.02:
            rotated = True
            dpi_w = h / (spec.full_width_mm / MM_PER_INCH)
            dpi_h = w / (spec.full_height_mm / MM_PER_INCH)
            r.warnings.append("la orientación de la imagen está girada respecto al producto")
        else:
            r.errors.append(f"proporción {w / h:.3f} no coincide con el producto "
                            f"{spec.full_width_mm / spec.full_height_mm:.3f}; Gelato recortaría")
    r.effective_dpi = int(min(dpi_w, dpi_h))
    r.width_mm = round(spec.full_width_mm, 1)
    r.height_mm = round(spec.full_height_mm, 1)
    if r.effective_dpi < spec.min_dpi:
        need = spec.min_pixels()
        r.errors.append(f"resolución efectiva {r.effective_dpi} dpi < {spec.min_dpi}; "
                        f"hacen falta al menos {need[0]}x{need[1]} px")
    elif r.effective_dpi < 300:
        r.warnings.append(f"resolución efectiva {r.effective_dpi} dpi; el óptimo es 300")
    r.ok = not r.errors
    return r


def spec_from_gelato_product(product: dict, product_uid: str, bleed_mm: float = 3.0,
                             kind: str = "flat", min_dpi: int = 150) -> ProductSpec | None:
    """Construye una ProductSpec a partir de la respuesta de GET /v3/products/{uid},
    que trae `dimensions` en mm (Width/Height) cuando el producto las publica."""
    dims = product.get("dimensions") or {}
    w = dims.get("Width", {}).get("value") if isinstance(dims.get("Width"), dict) else dims.get("Width")
    h = dims.get("Height", {}).get("value") if isinstance(dims.get("Height"), dict) else dims.get("Height")
    if not (w and h):
        return None
    return ProductSpec(product_uid, float(w), float(h), bleed_mm=bleed_mm, kind=kind, min_dpi=min_dpi)
