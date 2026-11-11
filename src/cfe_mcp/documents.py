"""Mandatory local metadata removal for outgoing documents; originals remain immutable."""

from __future__ import annotations

import base64
import hashlib
import io
import os
import re
import warnings
import zlib
from pathlib import Path

import pikepdf
from pikepdf import sanitize
from PIL import Image, ImageCms, ImageOps

from .client import CfeError

POLICY = "metadata-v1"
MAX_BYTES = 3 * 1024 * 1024
MAX_PIXELS = 40_000_000
PDF_METADATA_KEYS = {
    "/Metadata", "/PieceInfo", "/LastModified", "/CreationDate", "/ModDate",
    "/Creator", "/Producer", "/Author", "/Subject", "/Keywords",
    "/PTEX.FileName", "/PTEX.InfoDict", "/PTEX.Fullbanner",
}
ANNOTATION_TYPES = {
    "/Text", "/Link", "/FreeText", "/Line", "/Square", "/Circle", "/Polygon",
    "/PolyLine", "/Highlight", "/Underline", "/Squiggly", "/StrikeOut", "/Stamp",
    "/Caret", "/Ink", "/Popup", "/FileAttachment", "/Widget", "/Redact",
}


def jpeg_without_metadata(content: bytes) -> bytes:
    """Strip APP/COM segments and trailing data, preserving the compressed image scans."""
    if not content.startswith(b"\xff\xd8"):
        raise ValueError("Invalid JPEG")
    output, position = bytearray(content[:2]), 2
    while position < len(content):
        if content[position] != 255:
            raise ValueError("Invalid JPEG marker")
        while position < len(content) and content[position] == 255:
            position += 1
        marker = content[position]
        position += 1
        if marker == 0xD9:
            output.extend(b"\xff\xd9")
            return bytes(output)
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            output.extend(bytes((255, marker)))
            continue
        size = int.from_bytes(content[position:position + 2], "big")
        if size < 2 or position + size > len(content):
            raise ValueError("Invalid JPEG segment")
        payload = content[position + 2:position + size]
        # Adobe's 12-byte marker is necessary for CMYK/YCCK decoding, not user metadata.
        if marker == 0xEE and payload.startswith(b"Adobe") and len(payload) >= 12:
            output.extend(b"\xff\xee\x00\x0e" + payload[:12])
        elif not 0xE0 <= marker <= 0xEF and marker != 0xFE:
            output.extend(bytes((255, marker)) + content[position:position + size])
        position += size
        if marker == 0xDA:
            end = re.search(rb"\xff(?![\x00\xd0-\xd7])", content[position:])
            if end is None:
                raise ValueError("Unterminated JPEG scan")
            next_marker = position + end.start()
            output.extend(content[position:next_marker])
            position = next_marker
    raise ValueError("Missing JPEG end")


def image_pixels(content: bytes) -> tuple[tuple[int, int], str, bytes]:
    with Image.open(io.BytesIO(content)) as image:
        if image.width * image.height > MAX_PIXELS or getattr(image, "n_frames", 1) != 1:
            raise CfeError("Image trop grande ou animée : aucune transmission effectuée.")
        image.load()
        return image.size, image.mode, image.tobytes()


def clean_image(content: bytes, extension: str) -> tuple[bytes, str]:
    with Image.open(io.BytesIO(content)) as source:
        expected = "PNG" if extension == ".png" else "JPEG"
        if source.format != expected:
            raise ValueError("Image format mismatch")
        if source.width * source.height > MAX_PIXELS or getattr(source, "n_frames", 1) != 1:
            raise CfeError("Image trop grande ou animée : aucune transmission effectuée.")
        source.load()
        orientation = source.getexif().get(274, 1)
        if expected == "JPEG" and orientation == 1 and not source.info.get("icc_profile"):
            cleaned = jpeg_without_metadata(content)
            if image_pixels(cleaned) != (source.size, source.mode, source.tobytes()):
                raise CfeError("Le nettoyage JPEG modifierait les pixels ; envoi bloqué.")
            return cleaned, ".jpg"
        # Apply orientation/color information before deleting it. A fresh PNG preserves pixels
        # without the extra JPEG loss that would result from recompressing a rotated photograph.
        image = ImageOps.exif_transpose(source)
        profile = source.info.get("icc_profile")
        mode = "RGBA" if "A" in image.getbands() or "transparency" in image.info else "RGB"
        if profile:
            image = ImageCms.profileToProfile(
                image, io.BytesIO(profile), ImageCms.createProfile("sRGB"), outputMode=mode
            )
        image = image.convert(mode)
        plain = Image.frombytes(mode, image.size, image.tobytes())
        output = io.BytesIO()
        plain.save(output, format="PNG")
        cleaned = output.getvalue()
        if image_pixels(cleaned) != (plain.size, plain.mode, plain.tobytes()):
            raise CfeError("Le nettoyage PNG modifierait les pixels ; envoi bloqué.")
        return cleaned, ".png"


def pdf_objects(pdf):
    """Walk direct dictionaries too; metadata is not limited to the document catalog."""
    seen = set()
    pending = [(obj, 0) for obj in pdf.objects]
    count = 0
    while pending:
        obj, depth = pending.pop()
        if not isinstance(obj, (pikepdf.Dictionary, pikepdf.Stream, pikepdf.Array)):
            continue
        if depth > 80 or count > 100_000:
            raise CfeError("PDF trop complexe pour un nettoyage vérifiable.")
        if obj.is_indirect:
            if obj.objgen in seen:
                continue
            seen.add(obj.objgen)
        count += 1
        children = list(obj) if isinstance(obj, pikepdf.Array) else list(obj.values())
        pending.extend((child, depth + 1) for child in children)
        if not isinstance(obj, pikepdf.Array):
            yield obj


def pdf_jpeg_bytes(obj, filters):
    """Decode outer lossless filters while keeping the original JPEG compressed pixels."""
    if filters[-1] != "/DCTDecode" or filters.count("/DCTDecode") != 1:
        raise CfeError("Encodage JPEG composé non pris en charge ; envoi bloqué.")
    parameters = obj.get("/DecodeParms")
    params = list(parameters) if isinstance(parameters, pikepdf.Array) else [parameters]
    if len(params) == 1 and len(filters) > 1 and parameters is None:
        params = [None] * len(filters)
    if len(params) != len(filters):
        raise CfeError("Paramètres de décodage JPEG incohérents.")
    data = obj.read_raw_bytes()
    limit = MAX_BYTES * 8
    for filter_type, param in zip(filters[:-1], params[:-1], strict=True):
        if param is not None and param.get("/Predictor", 1) != 1:
            raise CfeError("Prédicteur JPEG composé non pris en charge ; envoi bloqué.")
        if filter_type == "/FlateDecode":
            inflater = zlib.decompressobj()
            data = inflater.decompress(data, limit + 1)
            if len(data) > limit or not inflater.eof:
                raise CfeError("Flux PDF trop volumineux ou invalide.")
        elif filter_type == "/ASCII85Decode":
            data = base64.a85decode(data.strip(), adobe=data.rstrip().endswith(b"~>"))
        elif filter_type == "/ASCIIHexDecode":
            encoded = re.sub(rb"\s", b"", data).removesuffix(b">")
            if len(encoded) % 2:
                encoded += b"0"
            data = bytes.fromhex(encoded.decode("ascii"))
        else:
            raise CfeError("Filtre PDF composé non pris en charge ; envoi bloqué.")
        if len(data) > limit:
            raise CfeError("Flux PDF trop volumineux pour le nettoyage.")
    return data, params[-1]


def clean_pdf(content: bytes) -> bytes:
    with pikepdf.Pdf.open(
        io.BytesIO(content), attempt_recovery=False, suppress_warnings=True
    ) as pdf:
        if pdf.is_encrypted:
            raise CfeError("PDF chiffré : fournir une copie non chiffrée avant transmission.")
        if not 1 <= len(pdf.pages) <= 100:
            raise CfeError("Le justificatif PDF doit contenir entre 1 et 100 pages.")
        objects = list(pdf_objects(pdf))
        for obj in objects:
            if (
                obj.get("/FT") == "/Sig"
                or obj.get("/Type") in ("/Sig", "/DocTimeStamp")
                or "/ByteRange" in obj
            ):
                raise CfeError(
                    "PDF avec signature numérique : nettoyage bloqué pour ne pas l'invalider. "
                    "Le document original reste intact."
                )
            if obj.get("/Type") == "/EmbeddedFile" or "/EF" in obj:
                raise CfeError(
                    "Ce PDF contient des fichiers incorporés. Les extraire et les joindre "
                    "séparément pour que leurs métadonnées soient aussi nettoyées."
                )
        if "/Info" in pdf.trailer:
            del pdf.trailer["/Info"]
        if "/ID" in pdf.trailer:
            del pdf.trailer["/ID"]
        sanitize.remove_private_app_data(pdf)
        sanitize.remove_web_capture(pdf)
        sanitize.remove_thumbnails(pdf)
        sanitize.remove_search_index(pdf)
        for obj in objects:
            for key in PDF_METADATA_KEYS & set(obj.keys()):
                del obj[key]
            if obj.get("/Type") == "/Metadata" and isinstance(obj, pikepdf.Stream):
                obj.write(b"")
            if obj.get("/Type") == "/Annot" or obj.get("/Subtype") in ANNOTATION_TYPES:
                for key in ("/T", "/M", "/NM", "/Subj"):
                    if key == "/T" and obj.get("/Subtype") == "/Widget":
                        continue  # A widget's /T is its form field name, not an author.
                    if key in obj:
                        del obj[key]
            if obj.get("/Subtype") == "/Image" and isinstance(obj, pikepdf.Stream):
                filter_type = obj.get("/Filter")
                filters = (
                    list(filter_type) if isinstance(filter_type, pikepdf.Array) else [filter_type]
                )
                if "/JPXDecode" in filters:
                    raise CfeError(
                        "Image JPEG 2000 intégrée au PDF non prise en charge ; envoi bloqué."
                    )
                if "/DCTDecode" in filters:
                    before, parameters = pdf_jpeg_bytes(obj, filters)
                    after = jpeg_without_metadata(before)
                    if image_pixels(before) != image_pixels(after):
                        raise CfeError("Les pixels d'une image du PDF changeraient ; envoi bloqué.")
                    obj.write(after, filter=pikepdf.Name.DCTDecode, decode_parms=parameters)
        output = io.BytesIO()
        # Full rewrite removes orphaned metadata and incremental revisions. A static technical
        # ID replaces the source document's identifier; no dates/producer are injected on save.
        pdf.save(
            output, static_id=True, fix_metadata_version=False,
            compress_streams=True, object_stream_mode=pikepdf.ObjectStreamMode.generate,
        )
    cleaned = output.getvalue()
    with pikepdf.Pdf.open(io.BytesIO(cleaned)) as check:
        if "/Info" in check.trailer or any(
            PDF_METADATA_KEYS & set(obj.keys()) for obj in pdf_objects(check)
        ):
            raise CfeError("Des métadonnées PDF subsistent ; transmission bloquée.")
    return cleaned


def sanitize_document(content: bytes, extension: str) -> tuple[bytes, str]:
    if not content or len(content) > MAX_BYTES:
        raise CfeError("Le justificatif doit être non vide et limité à 3 Mio.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            if extension.lower() == ".pdf":
                cleaned, extension = clean_pdf(content), ".pdf"
            elif extension.lower() in {".png", ".jpg", ".jpeg"}:
                cleaned, extension = clean_image(content, extension.lower())
            else:
                raise CfeError("Nettoyage accepté uniquement pour PDF, JPEG et PNG.")
    except CfeError:
        raise
    except Exception:
        # Parser failures can contain document strings: never include their raw error text.
        raise CfeError(
            "Nettoyage des métadonnées impossible ; aucune transmission effectuée."
        ) from None
    if len(cleaned) > MAX_BYTES:
        raise CfeError("La copie nettoyée dépasse 3 Mio ; aucune transmission effectuée.")
    return cleaned, extension


def delivery_document(content: bytes, attachment: dict, folder: Path) -> tuple[bytes, dict]:
    """Create/verify a derived private copy, including for drafts created before this policy."""
    if hashlib.sha256(content).hexdigest() != attachment["sha256"]:
        raise CfeError("Le justificatif source a changé ; nettoyage interrompu.")
    aid = attachment["id"]
    if not re.fullmatch(r"[0-9a-f]{32}", aid):
        raise CfeError("Identifiant de pièce invalide.")
    cleaned, extension = sanitize_document(content, Path(attachment["blob"]).suffix)
    name = f"piece-{aid[:8]}{extension}"
    path = folder / f"{aid}.{POLICY}{extension}"
    if path.is_symlink():
        raise CfeError("La copie nettoyée ne doit pas être un lien symbolique.")
    if path.exists():
        if path.read_bytes() != cleaned:
            raise CfeError("La copie nettoyée a été modifiée ; transmission bloquée.")
    else:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(cleaned)
            stream.flush()
            os.fsync(stream.fileno())
    return cleaned, {
        "policy": POLICY, "name": name, "path": str(path), "bytes": len(cleaned),
        "sha256": hashlib.sha256(cleaned).hexdigest(), "source_sha256": attachment["sha256"],
        "original_preserved": True, "metadata_stripped": True,
    }
