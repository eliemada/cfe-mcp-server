import base64
import hashlib
import io
import json
import zlib

import pikepdf
import pytest
from pdf_samples import sample_pdf
from PIL import Image, ImageCms, ImageOps, PngImagePlugin
from test_claims import CLAIM, claim_transport, count
from test_claims import invoice as invoice
from test_mobile_api import mobile_root as mobile_root

from cfe_mcp.claims import ClaimManager
from cfe_mcp.client import CfeError
from cfe_mcp.documents import POLICY, delivery_document, sanitize_document
from cfe_mcp.mobile_api import MobileApiClient


def saved(pdf):
    buffer = io.BytesIO()
    pdf.save(buffer, static_id=True, fix_metadata_version=False)
    return buffer.getvalue()


def jpeg(*, progressive=False, orientation=1, profile=False):
    image = Image.new("RGB", (20, 12), "white")
    image.paste("red", (0, 0, 6, 5))
    exif = Image.Exif()
    exif[274] = orientation
    exif[315] = "PRIVATE PHOTOGRAPHER"
    exif[306] = "2026:01:01 01:02:03"
    buffer = io.BytesIO()
    kwargs = {}
    if profile:
        kwargs["icc_profile"] = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    image.save(buffer, format="JPEG", exif=exif, progressive=progressive, **kwargs)
    raw = buffer.getvalue()
    comment = b"PRIVATE COMMENT"
    raw = raw[:2] + b"\xff\xfe" + (len(comment) + 2).to_bytes(2, "big") + comment + raw[2:]
    return raw + b"PRIVATE TRAILING DATA"


def test_pdf_removes_info_xmp_page_metadata_annotation_identity_and_old_data():
    with pikepdf.Pdf.open(io.BytesIO(sample_pdf())) as pdf:
        page = pdf.pages[0]
        original_graphics = page.Contents.read_bytes()
        pdf.Root.Metadata = pdf.make_stream(b"PRIVATE ROOT XMP")
        page.Metadata = pdf.make_stream(b"PRIVATE PAGE XMP")
        page.Resources.Private = pikepdf.Dictionary(
            Metadata=pdf.make_stream(b"PRIVATE NESTED XMP"),
            PieceInfo=pikepdf.Dictionary(Private=pikepdf.String("PRIVATE APP DATA")),
        )
        page.Annots = pikepdf.Array([pdf.make_indirect(pikepdf.Dictionary(
            Subtype=pikepdf.Name.Text, Rect=[20, 20, 30, 30],
            T="PRIVATE ANNOTATION AUTHOR", M="D:20260101000000Z",
            Contents="Visible note preserved",
        ))])
        original = saved(pdf) + b"\nPRIVATE OLD REVISION"
    cleaned, extension = sanitize_document(original, ".pdf")
    assert extension == ".pdf" and b"PRIVATE OLD REVISION" not in cleaned
    with pikepdf.Pdf.open(io.BytesIO(cleaned)) as pdf:
        assert "/Info" not in pdf.trailer and "/Prev" not in pdf.trailer
        assert "/Metadata" not in pdf.Root and "/Metadata" not in pdf.pages[0].obj
        assert pdf.pages[0].Contents.read_bytes() == original_graphics
        annotation = pdf.pages[0].Annots[0]
        assert "/T" not in annotation and "/M" not in annotation
        assert str(annotation.Contents) == "Visible note preserved"
        assert "/Metadata" not in pdf.pages[0].Resources.Private
    assert sanitize_document(original, ".pdf")[0] == cleaned


@pytest.mark.parametrize("progressive", [False, True])
def test_jpeg_removes_metadata_without_recompressing_pixels(progressive):
    original = jpeg(progressive=progressive)
    cleaned, extension = sanitize_document(original, ".jpg")
    assert extension == ".jpg" and b"PRIVATE" not in cleaned
    with Image.open(io.BytesIO(original)) as before, Image.open(io.BytesIO(cleaned)) as after:
        assert before.tobytes() == after.tobytes()
        assert not after.getexif() and "comment" not in after.info and "xmp" not in after.info


@pytest.mark.parametrize("profile", [False, True])
def test_jpeg_orientation_is_applied_before_metadata_is_removed(profile):
    original = jpeg(orientation=6, profile=profile)
    cleaned, extension = sanitize_document(original, ".jpeg")
    assert extension == ".png" and b"PRIVATE" not in cleaned
    with Image.open(io.BytesIO(original)) as before, Image.open(io.BytesIO(cleaned)) as after:
        assert after.size == (12, 20) and after.info == {}
        assert after.tobytes() == ImageOps.exif_transpose(before).tobytes()


def test_png_is_rebuilt_without_text_exif_or_source_filename():
    image = Image.new("RGBA", (12, 8), (20, 30, 40, 120))
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("Author", "PRIVATE PNG AUTHOR")
    metadata.add_itxt("XML:com.adobe.xmp", "PRIVATE XMP")
    exif = Image.Exif()
    exif[315] = "PRIVATE EXIF"
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", pnginfo=metadata, exif=exif)
    cleaned, extension = sanitize_document(buffer.getvalue(), ".png")
    with Image.open(io.BytesIO(cleaned)) as after:
        assert extension == ".png" and after.info == {}
        assert after.tobytes() == image.tobytes()
    assert b"PRIVATE" not in cleaned


@pytest.mark.parametrize("wrapper", [None, "FlateDecode", "ASCII85Decode", "ASCIIHexDecode"])
def test_pdf_embedded_jpeg_is_cleaned_and_decode_parameters_preserved(wrapper):
    with pikepdf.Pdf.open(io.BytesIO(sample_pdf())) as pdf:
        content = jpeg()
        if wrapper == "FlateDecode":
            content = zlib.compress(content)
        elif wrapper == "ASCII85Decode":
            content = base64.a85encode(content, adobe=True)
        elif wrapper == "ASCIIHexDecode":
            content = content.hex().encode() + b">"
        stream = pdf.make_stream(content)
        stream.Type = pikepdf.Name.XObject
        stream.Subtype = pikepdf.Name.Image
        stream.Width, stream.Height = 20, 12
        stream.BitsPerComponent = 8
        stream.ColorSpace = pikepdf.Name.DeviceRGB
        stream.Filter = pikepdf.Name.DCTDecode
        stream.DecodeParms = pikepdf.Dictionary(ColorTransform=0)
        if wrapper:
            stream.Filter = pikepdf.Array([pikepdf.Name('/' + wrapper), pikepdf.Name.DCTDecode])
            stream.DecodeParms = pikepdf.Array([None, pikepdf.Dictionary(ColorTransform=0)])
        pdf.pages[0].Resources.XObject = pikepdf.Dictionary(Im=stream)
        pdf.pages[0].Contents = pdf.make_stream(b"q 20 0 0 12 0 0 cm /Im Do Q")
        original = saved(pdf)
    cleaned, _ = sanitize_document(original, ".pdf")
    with pikepdf.Pdf.open(io.BytesIO(cleaned)) as pdf:
        image = pdf.pages[0].Resources.XObject.Im
        assert b"PRIVATE" not in image.read_raw_bytes()
        assert image.DecodeParms.ColorTransform == 0


@pytest.mark.parametrize("kind", ["signature", "embedded", "encrypted", "malformed"])
def test_unsupported_or_protected_documents_block_instead_of_falling_back(kind):
    with pikepdf.Pdf.open(io.BytesIO(sample_pdf())) as pdf:
        if kind == "signature":
            pdf.Root.AcroForm = pikepdf.Dictionary(Fields=pikepdf.Array([
                pdf.make_indirect(pikepdf.Dictionary(FT=pikepdf.Name.Sig, T="SIGNATURE"))
            ]))
        if kind == "embedded":
            pdf.attachments["hidden.txt"] = b"PRIVATE ATTACHMENT"
        original = saved(pdf)
        if kind == "encrypted":
            buffer = io.BytesIO()
            pdf.save(buffer, encryption=pikepdf.Encryption(owner="secret", user=""))
            original = buffer.getvalue()
        if kind == "malformed":
            original = b"%PDF-PRIVATE INVALID CONTENT"
    with pytest.raises(CfeError) as error:
        sanitize_document(original, ".pdf")
    assert "PRIVATE" not in str(error.value) and "secret" not in str(error.value)


def test_delivery_retains_original_hash_uses_generic_name_and_rejects_modified_copy(tmp_path):
    original = sample_pdf()
    attachment = {
        "id": "a" * 32, "blob": "a" * 32 + ".pdf",
        "name": "private-patient-date.pdf", "sha256": hashlib.sha256(original).hexdigest(),
    }
    cleaned, report = delivery_document(original, attachment, tmp_path)
    assert report["policy"] == POLICY and report["original_preserved"]
    assert report["source_sha256"] == attachment["sha256"]
    assert "private" not in report["name"]
    assert report["sha256"] == hashlib.sha256(cleaned).hexdigest()
    assert delivery_document(original, attachment, tmp_path) == (cleaned, report)
    from pathlib import Path
    Path(report["path"]).write_bytes(b"modified")
    with pytest.raises(CfeError, match="modifiée"):
        delivery_document(original, attachment, tmp_path)


async def test_claim_upload_transmits_only_cleaned_bytes(mobile_root, invoice):
    calls = []
    async with MobileApiClient(mobile_root, transport=claim_transport(calls)) as client:
        manager = ClaimManager(client)
        draft = await manager.prepare(CLAIM)
        draft = await manager.attach(draft["draft_id"], str(invoice), "facture")
        checked = await manager.validate(draft["draft_id"])
        assert checked["outbound_documents"][0]["metadata_stripped"]
        assert count(calls, "addDocument") == 0
        await manager.upload(draft["draft_id"])
    request = next(request for request in calls if request.url.path.endswith("_addDocument"))
    payload = json.loads(request.content)
    assert payload["file_name"].startswith("piece-")
    content = base64.b64decode(payload["file_content"])
    with pikepdf.Pdf.open(io.BytesIO(content)) as pdf:
        assert "/Info" not in pdf.trailer
    assert hashlib.sha256(content).hexdigest() == checked["outbound_documents"][0]["sha256"]


async def test_claim_cannot_submit_previously_uploaded_unsanitized_attachment(mobile_root, invoice):
    calls = []
    async with MobileApiClient(mobile_root, transport=claim_transport(calls)) as client:
        manager = ClaimManager(client)
        draft = await manager.prepare(CLAIM)
        draft = await manager.attach(draft["draft_id"], str(invoice), "facture")
        uploaded = await manager.upload(draft["draft_id"])
        stored = manager.load(draft["draft_id"], await client.account_context())
        del stored["attachments"][0]["sent_document"]
        manager.save(stored)
        with pytest.raises(CfeError, match="métadonnées"):
            await manager.submit(draft["draft_id"], uploaded["revision"])
        assert count(calls, "sendRequest") == 0


async def test_invalid_pdf_never_reaches_server_care_validation_or_upload(mobile_root, invoice):
    invoice.write_bytes(b"%PDF-1.4\nINVALID PRIVATE DOCUMENT")
    calls = []
    async with MobileApiClient(mobile_root, transport=claim_transport(calls)) as client:
        manager = ClaimManager(client)
        draft = await manager.prepare(CLAIM)
        draft = await manager.attach(draft["draft_id"], str(invoice), "facture")
        with pytest.raises(CfeError, match="métadonnées"):
            await manager.upload(draft["draft_id"])
        assert count(calls, "checkCare") == count(calls, "addDocument") == 0
