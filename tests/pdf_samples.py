"""Valid synthetic PDFs, never suitable for production submission."""

import io

import pikepdf


def sample_pdf(label="OFFLINE SYNTHETIC TEST ONLY"):
    with pikepdf.Pdf.new() as pdf:
        page = pdf.add_blank_page(page_size=(300, 200))
        page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(
            F1=pikepdf.Dictionary(Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1,
                                  BaseFont=pikepdf.Name.Helvetica)
        ))
        page.Contents = pdf.make_stream(f"BT /F1 10 Tf 20 100 Td ({label}) Tj ET".encode())
        pdf.docinfo["/Author"] = "PRIVATE TEST AUTHOR"
        pdf.docinfo["/CreationDate"] = "D:20260101000000Z"
        buffer = io.BytesIO()
        pdf.save(buffer, static_id=True)
        return buffer.getvalue()
