"""
Invoice PDF renderer — matches the public web view in needed.pdf plus
a grid-based payment section and a page-end company footer.

Layout rules:
  · One page preferred. A simple invoice fits on one letter page.
  · Payment cards are laid out in a responsive grid:
      1 method  → full width (centred, current design)
      2 methods → side-by-side
      3-4       → 2 × 2 grid
      5+        → 2 columns, more rows
    Inside every card the layout is column-stacked: label ABOVE
    value, not two columns side-by-side; QR centred at the bottom,
    instructions below the QR.
  · Company address / phone / tax id appear at the BOTTOM of the
    very last page as a footer band — not repeated on every page and
    not shown in the top header.
  · The top header keeps the logo, company name, INVOICE label and
    number.
  · Images (logo, QR) load from local paths OR remote URLs
    (Cloudinary) — `FieldFile.path` raises on Cloudinary storage, so
    we download `.url` to a temp file when needed.
"""
import io
import logging
import os
import tempfile
from decimal import Decimal

from django.conf import settings
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.enums import TA_RIGHT, TA_LEFT, TA_CENTER
from reportlab.platypus import (
    BaseDocTemplate, PageTemplate, Frame,
    Paragraph, Spacer, Table, TableStyle, Image,
    HRFlowable, KeepTogether,
)

log = logging.getLogger(__name__)

# ── Theme (matches Tailwind tokens in PublicInvoicePage.jsx) ───────
INK_HEADING = colors.HexColor("#0e1b2b")
INK_BODY    = colors.HexColor("#1f2937")
INK_MUTED   = colors.HexColor("#6b7280")
INK_LIGHT   = colors.HexColor("#9ca3af")
INK_BORDER  = colors.HexColor("#e5e7eb")
INK_BORDER_STRONG = colors.HexColor("#374151")
BRAND_SOFT  = colors.HexColor("#ecf3ef")
BRAND_RIM   = colors.HexColor("#cde0d5")
BRAND_HEAD  = colors.HexColor("#0e5a3a")


def _fmt_money(amount, currency="USD"):
    try:
        n = Decimal(str(amount or 0)).quantize(Decimal("0.01"))
    except Exception:
        n = Decimal("0.00")
    if currency == "USD":
        return f"$ {n:,.2f}"
    return f"{currency} {n:,.2f}"


def _safe(value, fallback=""):
    return str(value) if value not in (None, "") else fallback


# ── Image resolution (local + remote/Cloudinary) ───────────────────
_REMOTE_CACHE: dict = {}


def _download_to_temp(url):
    if not url:
        return None
    if url in _REMOTE_CACHE:
        return _REMOTE_CACHE[url]
    try:
        import requests
        r = requests.get(url, timeout=8, stream=True)
        if r.status_code != 200:
            log.warning("invoice pdf: image fetch %s returned %s",
                        url, r.status_code)
            _REMOTE_CACHE[url] = None
            return None
        ct = (r.headers.get("content-type") or "").lower()
        ext = ".png"
        if "jpeg" in ct or "jpg" in ct:
            ext = ".jpg"
        elif "gif" in ct:
            ext = ".gif"
        elif "webp" in ct:
            ext = ".webp"
        fd, path = tempfile.mkstemp(suffix=ext, prefix="inv_img_")
        try:
            with os.fdopen(fd, "wb") as fh:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        fh.write(chunk)
        except Exception:
            try:
                os.unlink(path)
            except Exception:
                pass
            raise
        _REMOTE_CACHE[url] = path
        return path
    except Exception as e:
        log.warning("invoice pdf: image download failed %s: %s", url, e)
        _REMOTE_CACHE[url] = None
        return None


def _resolve_image_path(candidate):
    if not candidate:
        return None
    candidate = str(candidate)
    try:
        if os.path.isabs(candidate) and os.path.exists(candidate):
            return candidate
    except Exception:
        pass
    if candidate.startswith(("http://", "https://")):
        try:
            media_url = (settings.MEDIA_URL or "").rstrip("/")
            media_root = settings.MEDIA_ROOT
            if media_url:
                idx = candidate.find(media_url)
                if idx != -1:
                    rel = candidate[idx + len(media_url):].lstrip("/")
                    p = os.path.join(media_root, rel)
                    if os.path.exists(p):
                        return p
        except Exception:
            pass
        return _download_to_temp(candidate)
    try:
        media_root = settings.MEDIA_ROOT
        p = os.path.join(media_root, candidate.lstrip("/"))
        if os.path.exists(p):
            return p
    except Exception as e:
        log.warning("image path resolve error for %r: %s", candidate, e)
    log.warning("image path did not resolve: candidate=%r", candidate)
    return None


def _safe_file_url(fieldfile):
    try:
        if fieldfile and getattr(fieldfile, "url", None):
            return fieldfile.url
    except Exception as e:
        log.warning("fieldfile.url failed: %s", e)
    return None


def _get_logo_path(invoice):
    try:
        if invoice.company and invoice.company.logo:
            try:
                p = invoice.company.logo.path
                if p and os.path.exists(p):
                    return p
            except Exception:
                pass
            url = _safe_file_url(invoice.company.logo)
            if url:
                resolved = _resolve_image_path(url)
                if resolved:
                    return resolved
    except Exception as e:
        log.warning("invoice %s: reading live logo failed: %s",
                    invoice.number, e)
    comp = invoice.company_snapshot or {}
    return (_resolve_image_path(comp.get("logo_url"))
            or _resolve_image_path(comp.get("logo_path")))


def _get_qr_path(pm_snapshot, live_method=None):
    if live_method is not None:
        try:
            if live_method.qr_code:
                try:
                    p = live_method.qr_code.path
                    if p and os.path.exists(p):
                        return p
                except Exception:
                    pass
                url = _safe_file_url(live_method.qr_code)
                if url:
                    resolved = _resolve_image_path(url)
                    if resolved:
                        return resolved
        except Exception as e:
            log.warning("qr live read failed: %s", e)
    return (_resolve_image_path((pm_snapshot or {}).get("qr_code_url"))
            or _resolve_image_path((pm_snapshot or {}).get("qr_code_path")))


# ── Page-end company footer ────────────────────────────────────────
#
# We do a two-pass build:
#   Pass 1 — build once with onPage = no-op to discover the final
#            page count.
#   Pass 2 — rebuild, drawing the company footer ONLY on the page
#            whose number equals the final page count.
#
# This guarantees the footer appears only on the last page, no
# matter how many pages the content runs across.

def _draw_company_footer(canvas, doc, comp):
    """Draw a subtle company-contact band at the bottom of the page."""
    canvas.saveState()
    width, _ = doc.pagesize

    # Build lines.
    addr_parts = [
        _safe(comp.get("address_line1")),
        _safe(comp.get("address_line2")),
    ]
    city_line = ", ".join(filter(None, [
        comp.get("city"), comp.get("state"),
        comp.get("postal_code"), comp.get("country"),
    ]))
    if city_line:
        addr_parts.append(city_line)
    addr = " · ".join(p for p in addr_parts if p)

    contact_parts = []
    for k in ("email", "phone", "website"):
        if comp.get(k):
            contact_parts.append(_safe(comp.get(k)))
    if comp.get("tax_id"):
        contact_parts.append(f"Tax ID: {_safe(comp.get('tax_id'))}")
    contact = " · ".join(contact_parts)

    if not addr and not contact:
        canvas.restoreState()
        return

    y_rule = 0.55 * inch
    canvas.setStrokeColor(INK_BORDER)
    canvas.setLineWidth(0.4)
    canvas.line(0.55 * inch, y_rule, width - 0.55 * inch, y_rule)

    canvas.setFillColor(INK_MUTED)
    canvas.setFont("Helvetica", 8)
    y = y_rule - 14
    if addr:
        canvas.drawCentredString(width / 2.0, y, addr)
        y -= 11
    if contact:
        canvas.drawCentredString(width / 2.0, y, contact)
    canvas.restoreState()


def _make_on_page_handler(comp, total_pages_box):
    """Return a reportlab onPage callback that draws the company
    footer only on the final page.

    `total_pages_box` is a list of length 1 — the second pass reads
    it to know which page is the last. In the first pass it's empty
    so we draw nothing.
    """
    def _on_page(canvas, doc):
        if not total_pages_box:
            return
        if canvas.getPageNumber() == total_pages_box[0]:
            _draw_company_footer(canvas, doc, comp)
    return _on_page


# ── Story builder (shared between the two passes) ──────────────────

def _build_story(invoice, styles):
    """Build the flowable list for the invoice body (no page chrome)."""
    comp = invoice.company_snapshot or {}
    client = invoice.client_snapshot or {}

    story = []

    # ── Top header: logo + company name on the left, INVOICE/number
    #    on the right. The company address / phone / tax id block
    #    that USED to live here is now drawn as a page-end footer
    #    instead, per the product request.
    logo_path = _get_logo_path(invoice)
    logo_cell = ""
    if logo_path:
        try:
            logo_cell = Image(logo_path, width=0.95 * inch,
                              height=0.95 * inch, kind="proportional")
        except Exception as e:
            log.warning("invoice pdf: logo load failed path=%r err=%s",
                        logo_path, e)
            logo_cell = ""

    name_stack = [Paragraph(_safe(comp.get("name"), "Invoice"),
                            styles["CompanyName"])]

    header_left = Table(
        [[logo_cell, name_stack]],
        colWidths=[1.1 * inch if logo_path else 0.01 * inch, None],
    )
    header_left.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING",  (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (0, 0), 12),
        ("RIGHTPADDING", (1, 0), (-1, -1), 0),
        ("TOPPADDING",   (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 0),
    ]))

    right_items = [
        Paragraph("INVOICE", styles["InvTitleRight"]),
        Paragraph(_safe(invoice.number, "INV-???"), styles["InvNumber"]),
        Paragraph(
            f"Issued {invoice.issue_date.strftime('%b %d, %Y')}"
            if invoice.issue_date else "Issued —",
            styles["InvMutedRight"],
        ),
    ]
    if invoice.due_date:
        right_items.append(Paragraph(
            f"Due {invoice.due_date.strftime('%b %d, %Y')}",
            styles["InvMutedRight"],
        ))

    header = Table(
        [[header_left, right_items]],
        colWidths=[4.2 * inch, 3.0 * inch],
    )
    header.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING",  (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING",   (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 0),
    ]))
    story.append(header)
    story.append(Spacer(1, 8))
    story.append(HRFlowable(width="100%", thickness=0.6,
                            color=INK_BORDER_STRONG))
    story.append(Spacer(1, 10))

    # ── Bill-to ────────────────────────────────────────────────────
    story.append(Paragraph("BILL TO", styles["SectionLabel"]))
    story.append(Spacer(1, 2))
    story.append(Paragraph(
        _safe(client.get("name"), "Client"), styles["InvBodyBold"],
    ))
    if client.get("company_name"):
        story.append(Paragraph(_safe(client.get("company_name")),
                               styles["InvMuted"]))
    if client.get("address"):
        story.append(Paragraph(
            _safe(client.get("address")).replace("\n", "<br/>"),
            styles["InvMuted"],
        ))
    if client.get("email"):
        story.append(Paragraph(_safe(client.get("email")),
                               styles["InvMuted"]))
    if client.get("phone"):
        story.append(Paragraph(_safe(client.get("phone")),
                               styles["InvMuted"]))
    story.append(Spacer(1, 10))

    # ── Summary box ────────────────────────────────────────────────
    if invoice.general_description:
        summary = Table([[Paragraph(
            _safe(invoice.general_description).replace("\n", "<br/>"),
            styles["InvBody"],
        )]], colWidths=[None])
        summary.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8f8f5")),
            ("BOX", (0, 0), (-1, -1), 0.4, INK_BORDER),
            ("LEFTPADDING", (0, 0), (-1, -1), 12),
            ("RIGHTPADDING", (0, 0), (-1, -1), 12),
            ("TOPPADDING", (0, 0), (-1, -1), 10),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ]))
        story.append(summary)
        story.append(Spacer(1, 12))

    # ── Line items ─────────────────────────────────────────────────
    def _th(text, align=TA_LEFT):
        return Paragraph(text, ParagraphStyle(
            "th", parent=styles["SectionLabel"], alignment=align,
            fontSize=8, textColor=INK_MUTED,
        ))

    header_row = [
        _th("#"), _th("ITEM"), _th("QTY", TA_RIGHT),
        _th("UNIT", TA_RIGHT), _th("TOTAL", TA_RIGHT),
    ]
    data = [header_row]
    for i, li in enumerate(invoice.line_items.all(), start=1):
        name_cell = Paragraph(_safe(li.name), styles["InvMuted"])
        if li.description:
            name_cell = Paragraph(
                f"{_safe(li.name)}<br/>"
                f"<font size=8 color='#9ca3af'>"
                f"{_safe(li.description).replace(chr(10), '<br/>')}</font>",
                styles["InvMuted"],
            )
        data.append([
            Paragraph(str(i), ParagraphStyle(
                "idx", parent=styles["InvMuted"], alignment=TA_LEFT,
            )),
            name_cell,
            Paragraph(f"{li.quantity:,.2f}", ParagraphStyle(
                "qty", parent=styles["InvMuted"], alignment=TA_RIGHT,
            )),
            Paragraph(
                _fmt_money(li.unit_price, invoice.currency_code),
                ParagraphStyle("unit", parent=styles["InvMuted"],
                               alignment=TA_RIGHT),
            ),
            Paragraph(
                _fmt_money(li.total, invoice.currency_code),
                ParagraphStyle("tot", parent=styles["InvBodyBold"],
                               alignment=TA_RIGHT),
            ),
        ])

    items_table = Table(
        data,
        colWidths=[0.35 * inch, 3.6 * inch, 0.7 * inch,
                   1.15 * inch, 1.45 * inch],
        repeatRows=1,
    )
    items_table.setStyle(TableStyle([
        ("LINEBELOW",     (0, 0), (-1, 0), 0.7, INK_BORDER_STRONG),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 7),
        ("TOPPADDING",    (0, 0), (-1, 0), 4),
        ("VALIGN",        (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW",     (0, -1), (-1, -1), 0.5, INK_BORDER_STRONG),
        ("TOPPADDING",    (0, 1), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 9),
        ("LEFTPADDING",   (0, 0), (-1, -1), 0),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 0),
    ]))
    story.append(items_table)
    story.append(Spacer(1, 6))

    # ── Totals ─────────────────────────────────────────────────────
    totals_rows = [[
        Paragraph("Subtotal", styles["InvBody"]),
        Paragraph(_fmt_money(invoice.subtotal, invoice.currency_code),
                  ParagraphStyle("v", parent=styles["InvBody"],
                                 alignment=TA_RIGHT)),
    ]]
    if invoice.tax_percent and Decimal(str(invoice.tax_percent)) > 0:
        totals_rows.append([
            Paragraph(f"Tax ({invoice.tax_percent}%)", styles["InvBody"]),
            Paragraph(_fmt_money(invoice.tax_amount, invoice.currency_code),
                      ParagraphStyle("v", parent=styles["InvBody"],
                                     alignment=TA_RIGHT)),
        ])
    totals_rows.append([
        Paragraph("Total", styles["TotalLabel"]),
        Paragraph(_fmt_money(invoice.total, invoice.currency_code),
                  styles["TotalValue"]),
    ])
    totals = Table(totals_rows, colWidths=[1.6 * inch, 1.6 * inch],
                   hAlign="RIGHT")
    totals.setStyle(TableStyle([
        ("LINEBELOW",  (0, 0), (-1, -2), 0.4, INK_BORDER),
        ("LINEABOVE",  (0, -1), (-1, -1), 0.6, INK_BORDER_STRONG),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING",   (0, 0), (-1, -1), 4),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 4),
    ]))
    story.append(totals)

    # ── Payment methods grid ───────────────────────────────────────
    # For single-method invoices we bundle the card + notes +
    # "Powered by PayBitnex" into ONE KeepTogether block so reportlab
    # treats them as atomic. Without this, the footer line can
    # orphan to the next page when the card pushes content down —
    # a common complaint on QR-heavy invoices where the payment
    # card is tall.
    methods = _collect_method_snapshots(invoice)

    # Build notes flowable list once; reused below.
    notes_flowables = []
    if invoice.notes:
        notes_flowables = [
            Spacer(1, 6),
            HRFlowable(width="100%", thickness=0.4, color=INK_BORDER),
            Spacer(1, 6),
            Paragraph(
                _safe(invoice.notes).replace("\n", "<br/>"),
                styles["InvMuted"],
            ),
        ]

    footer_flowables = [
        Spacer(1, 10),
        Paragraph("Powered by PayBitnex", styles["Footer"]),
    ]

    if methods:
        story.append(Spacer(1, 12))
        cols = 1 if len(methods) == 1 else 2

        if cols == 1:
            # Single method — bundle card + notes + footer line
            # together so they can't split across pages.
            bundle = [_build_method_card(methods[0], styles, single=True)]
            bundle.extend(notes_flowables)
            bundle.extend(footer_flowables)
            story.append(KeepTogether(bundle))
        else:
            # 2+ methods — keep the grid approach (each card atomic,
            # rows may break between cards). Notes + footer trail
            # naturally; if they orphan here it's less visually
            # disruptive than splitting a card.
            raw_cards = [
                _build_method_card(pm_entry, styles, single=False)
                for pm_entry in methods
            ]
            while len(raw_cards) % cols != 0:
                raw_cards.append("")
            rows = [raw_cards[i:i + cols]
                    for i in range(0, len(raw_cards), cols)]
            avail = 7.3 * inch
            col_w = avail / cols
            grid = Table(rows, colWidths=[col_w] * cols)
            grid.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING",  (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING",   (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING",(0, 0), (-1, -1), 12),
            ]))
            story.append(grid)
            story.extend(notes_flowables)
            story.extend(footer_flowables)
    else:
        # No payment methods at all — still show notes + footer.
        story.extend(notes_flowables)
        story.extend(footer_flowables)

    return story


# ── Main renderer ──────────────────────────────────────────────────

def render_invoice_pdf(invoice):
    """Return a BytesIO containing the rendered invoice PDF.

    Uses a two-pass build so the company-contact footer appears only
    on the final page regardless of content length.
    """
    _REMOTE_CACHE.clear()
    comp = invoice.company_snapshot or {}

    left = 0.55 * inch
    right = 0.55 * inch
    top_margin = 0.45 * inch
    bottom_margin = 0.75 * inch   # reserve space for page-end footer

    styles = _build_styles()

    # ── Pass 1: build once to discover total page count.
    total_pages_box = []   # populated after pass 1
    tmp_buf = io.BytesIO()
    doc1 = BaseDocTemplate(
        tmp_buf, pagesize=letter,
        leftMargin=left, rightMargin=right,
        topMargin=top_margin, bottomMargin=bottom_margin,
        title=f"Invoice {invoice.number}",
        author=_safe(comp.get("name"), "Invoice"),
    )
    frame1 = Frame(
        left, bottom_margin,
        doc1.pagesize[0] - left - right,
        doc1.pagesize[1] - top_margin - bottom_margin,
        id="body", showBoundary=0,
    )
    doc1.addPageTemplates([PageTemplate(
        id="main", frames=[frame1], onPage=lambda c, d: None,
    )])
    doc1.build(_build_story(invoice, styles))
    total_pages_box.append(doc1.page)   # final page count

    # ── Pass 2: build for real, drawing the footer on the last page.
    buf = io.BytesIO()
    doc2 = BaseDocTemplate(
        buf, pagesize=letter,
        leftMargin=left, rightMargin=right,
        topMargin=top_margin, bottomMargin=bottom_margin,
        title=f"Invoice {invoice.number}",
        author=_safe(comp.get("name"), "Invoice"),
    )
    frame2 = Frame(
        left, bottom_margin,
        doc2.pagesize[0] - left - right,
        doc2.pagesize[1] - top_margin - bottom_margin,
        id="body", showBoundary=0,
    )
    doc2.addPageTemplates([PageTemplate(
        id="main", frames=[frame2],
        onPage=_make_on_page_handler(comp, total_pages_box),
    )])
    doc2.build(_build_story(invoice, styles))
    buf.seek(0)
    return buf


def _build_styles():
    base = getSampleStyleSheet()
    return {
        "InvTitleRight": ParagraphStyle(
            "InvTitleRight", parent=base["BodyText"], alignment=TA_RIGHT,
            fontSize=11, textColor=INK_MUTED, fontName="Helvetica",
            leading=13, spaceAfter=2,
        ),
        "InvNumber": ParagraphStyle(
            "InvNumber", parent=base["BodyText"], alignment=TA_RIGHT,
            fontSize=24, textColor=INK_HEADING, fontName="Helvetica",
            leading=28, spaceAfter=4,
        ),
        "InvMutedRight": ParagraphStyle(
            "InvMutedRight", parent=base["BodyText"],
            fontSize=9, textColor=INK_MUTED, alignment=TA_RIGHT, leading=12,
        ),
        "SectionLabel": ParagraphStyle(
            "SectionLabel", parent=base["BodyText"],
            fontSize=8, textColor=INK_MUTED,
            fontName="Helvetica", spaceAfter=3,
        ),
        "InvBody": ParagraphStyle(
            "InvBody", parent=base["BodyText"],
            fontSize=10, textColor=INK_BODY, leading=13,
        ),
        "InvBodyBold": ParagraphStyle(
            "InvBodyBold", parent=base["BodyText"],
            fontSize=10.5, textColor=INK_HEADING, leading=14,
            fontName="Helvetica-Bold",
        ),
        "InvMuted": ParagraphStyle(
            "InvMuted", parent=base["BodyText"],
            fontSize=9, textColor=INK_MUTED, leading=12,
        ),
        "CompanyName": ParagraphStyle(
            "CompanyName", parent=base["BodyText"],
            fontSize=22, textColor=INK_LIGHT, leading=26,
            fontName="Helvetica",
        ),
        "PayHeadLabel": ParagraphStyle(
            "PayHeadLabel", parent=base["BodyText"],
            fontSize=7.5, textColor=BRAND_HEAD,
            fontName="Helvetica-Bold", leading=10, spaceAfter=4,
            alignment=TA_LEFT,
        ),
        "PayMethodName": ParagraphStyle(
            "PayMethodName", parent=base["BodyText"],
            fontSize=14, textColor=INK_LIGHT,
            fontName="Helvetica", leading=16, spaceAfter=6,
        ),
        "PayKey": ParagraphStyle(
            "PayKey", parent=base["BodyText"],
            fontSize=8, textColor=INK_MUTED, leading=10,
            fontName="Helvetica",
        ),
        "PayVal": ParagraphStyle(
            "PayVal", parent=base["BodyText"],
            fontSize=9.5, textColor=BRAND_HEAD, leading=12,
            fontName="Helvetica-Bold",
        ),
        "PayItalicSmall": ParagraphStyle(
            "PayItalicSmall", parent=base["BodyText"],
            fontSize=8.5, textColor=INK_MUTED, leading=11,
            fontName="Helvetica-Oblique", alignment=TA_CENTER,
        ),
        "Footer": ParagraphStyle(
            "Footer", parent=base["BodyText"],
            fontSize=9, textColor=INK_LIGHT,
            alignment=TA_CENTER, leading=12,
        ),
        "TotalLabel": ParagraphStyle(
            "TotalLabel", parent=base["BodyText"],
            fontSize=14, textColor=INK_HEADING,
            fontName="Helvetica", leading=18,
        ),
        "TotalValue": ParagraphStyle(
            "TotalValue", parent=base["BodyText"],
            fontSize=14, textColor=INK_HEADING,
            fontName="Helvetica", leading=18, alignment=TA_RIGHT,
        ),
    }


def _collect_method_snapshots(invoice):
    ipms = list(invoice.invoice_payment_methods.all().order_by(
        "position", "id",
    ))
    if ipms:
        return [(ipm.snapshot or {}, ipm.payment_method) for ipm in ipms]
    if invoice.payment_method_snapshot:
        return [(invoice.payment_method_snapshot, invoice.payment_method)]
    return []


def _build_method_card(pm_entry, styles, single):
    """Build one payment card as a KeepTogether flowable.

    Layout inside the card — MATCHES the web design requested:
      PAYMENT DETAILS   (small bold brand label)
      Zelle             (large light method name)
      ─────
      Account title          ← label
      Freight Flow Solutions ← value, on its own row
      Email
      freightflowsol@gmail.com
      Phone
      210-740-5653
      …
      ─────
      [   QR   ]          ← centred
      Scan with your app  ← centred italic under QR
      "Instructions…"     ← centred italic, slightly smaller
    """
    pm, live_pm = pm_entry if isinstance(pm_entry, tuple) else (pm_entry, None)

    body_rows = [
        [Paragraph("PAYMENT DETAILS", styles["PayHeadLabel"])],
        [Paragraph(_safe(pm.get("label"), "Payment"),
                   styles["PayMethodName"])],
    ]

    # Stacked key + value — labels on one row, value on the next.
    # We build a mini-table with one column and two paragraphs per
    # pair. Passing a Python list as a cell value doesn't work in
    # reportlab (it can't measure height), so we nest a Table.
    details_rows = []
    for key, disp in [
        ("holder_name",    "Account title"),
        ("email",          "Email"),
        ("phone",          "Phone"),
        ("cashapp_tag",    "Cashtag"),
        ("account_number", "Account #"),
        ("routing_number", "Routing #"),
        ("bank_name",      "Bank"),
        ("account_type",   "Type"),
    ]:
        v = pm.get(key)
        if v:
            details_rows.append([Paragraph(disp, styles["PayKey"])])
            details_rows.append([Paragraph(_safe(v), styles["PayVal"])])

    if details_rows:
        details_table = Table(details_rows, colWidths=[None])
        details_table.setStyle(TableStyle([
            ("LEFTPADDING",  (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            # Label rows: tight bottom padding so the value hugs
            # the label; value rows: slightly more bottom padding
            # so pairs are visually grouped.
            ("TOPPADDING",   (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING",(0, 0), (-1, -1), 1),
            # Every even-index row is a label, odd-index is a value.
        ]))
        # Give each value row a little bottom padding to separate pairs.
        extra = []
        for i in range(1, len(details_rows), 2):
            extra.append(("BOTTOMPADDING", (0, i), (0, i), 6))
        details_table.setStyle(TableStyle([
            *extra,
        ]))
        body_rows.append([details_table])

    # QR + scan text + instructions, centred and stacked. Same
    # reason as above: wrap in a Table, not a raw Python list.
    qr_rows = []
    qr_path = _get_qr_path(pm, live_pm)
    if qr_path:
        try:
            qr_size = 1.15 * inch if single else 0.95 * inch
            qr_img = Image(qr_path, width=qr_size, height=qr_size,
                           kind="proportional")
            # Set hAlign on the Image flowable itself — this makes
            # reportlab centre the image within its container even
            # when the container is wider than the image.
            qr_img.hAlign = "CENTER"
            qr_rows.append([qr_img])
            qr_rows.append([Paragraph(
                f"<i>Scan with your {_safe(pm.get('label'))} app</i>",
                styles["PayItalicSmall"],
            )])
        except Exception as e:
            log.warning("invoice pdf qr load failed path=%r err=%s",
                        qr_path, e)

    if pm.get("instructions"):
        qr_rows.append([Paragraph(
            f'<i>"{_safe(pm.get("instructions"))}"</i>',
            styles["PayItalicSmall"],
        )])

    if qr_rows:
        qr_table = Table(qr_rows, colWidths=[None])
        qr_table.setStyle(TableStyle([
            ("LEFTPADDING",  (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING",   (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING",(0, 0), (-1, -1), 2),
        ]))
        body_rows.append([qr_table])

    card = Table(body_rows, colWidths=[None])
    card.setStyle(TableStyle([
        ("BACKGROUND",   (0, 0), (-1, -1), BRAND_SOFT),
        ("BOX",          (0, 0), (-1, -1), 0.6, BRAND_RIM),
        ("LEFTPADDING",  (0, 0), (-1, -1), 14),
        ("RIGHTPADDING", (0, 0), (-1, -1), 14),
        ("TOPPADDING",   (0, 0), (0, 0), 14),
        ("TOPPADDING",   (0, 1), (-1, -1), 0),
        ("BOTTOMPADDING",(0, 0), (-1, -2), 6),
        ("BOTTOMPADDING",(0, -1), (-1, -1), 14),
        ("VALIGN",       (0, 0), (-1, -1), "TOP"),
    ]))
    return card
