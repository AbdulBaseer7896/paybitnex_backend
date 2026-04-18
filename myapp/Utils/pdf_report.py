"""
PayBitnex branded PDF report builder.

Public API:
    PDFReportBuilder(title, subtitle="", metadata={}).build(sections) -> bytes

Each `section` is a dict describing what to render:
    {"type": "heading",   "text": "Partner payouts"}
    {"type": "paragraph", "text": "Explanation paragraph..."}
    {"type": "kpi_grid",  "items": [{"label": "X", "value": "Y"}, ...]}
    {"type": "table",     "headers": [...], "rows": [[...], ...],
                          "col_widths": [2,3,1,1,1],   # optional, in inches
                          "align": [...],              # optional per column
                          "total_row": [...]}          # optional footer row
    {"type": "spacer",    "height": 12}

The builder stamps:
  · A branded header on every page (company name + tagline + horizontal rule)
  · A footer on every page with "Generated on <timestamp>" + "Page N of M"
  · A cover block at the top of page 1 (title, subtitle, metadata grid)

Everything uses the palette/typography tokens from the PayBitnex web app so
printed output feels visually connected to the screen UI.
"""
from decimal import Decimal
from io import BytesIO
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch, mm
from reportlab.platypus import (
    BaseDocTemplate, Frame, PageTemplate, Paragraph, Spacer, Table, TableStyle,
    PageBreak, KeepTogether,
)
from reportlab.pdfgen import canvas
from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER


# ── Brand palette ────────────────────────────────────────────────────
BRAND_DARK  = colors.HexColor("#0F3D30")   # deep brand green
BRAND_MID   = colors.HexColor("#12694E")
BRAND_LITE  = colors.HexColor("#E7F1EC")
INK_900     = colors.HexColor("#101115")
INK_700     = colors.HexColor("#3A3D44")
INK_500     = colors.HexColor("#6B6F78")
INK_300     = colors.HexColor("#C7CAD1")
INK_100     = colors.HexColor("#EEEFF2")
CREAM       = colors.HexColor("#FAF8F3")
AMBER_700   = colors.HexColor("#B45309")
ROSE_700    = colors.HexColor("#B91C1C")


def _build_styles():
    base = getSampleStyleSheet()
    return {
        "title":     ParagraphStyle("title", parent=base["Title"],
                                    textColor=INK_900, fontSize=22,
                                    leading=26, spaceAfter=4),
        "subtitle":  ParagraphStyle("subtitle", parent=base["Normal"],
                                    textColor=INK_500, fontSize=11,
                                    leading=14, spaceAfter=14),
        "heading":   ParagraphStyle("heading", parent=base["Heading2"],
                                    textColor=BRAND_DARK, fontSize=13,
                                    leading=16, spaceBefore=16, spaceAfter=6,
                                    fontName="Helvetica-Bold"),
        "subheading": ParagraphStyle("subheading", parent=base["Heading3"],
                                     textColor=INK_700, fontSize=10,
                                     leading=13, spaceBefore=8, spaceAfter=4,
                                     fontName="Helvetica-Bold",
                                     textTransform="uppercase"),
        "body":      ParagraphStyle("body", parent=base["Normal"],
                                    textColor=INK_700, fontSize=9.5,
                                    leading=13, spaceAfter=4),
        "small":     ParagraphStyle("small", parent=base["Normal"],
                                    textColor=INK_500, fontSize=8,
                                    leading=11),
        "meta_key":  ParagraphStyle("meta_key", parent=base["Normal"],
                                    textColor=INK_500, fontSize=8,
                                    leading=11, spaceAfter=1,
                                    textTransform="uppercase"),
        "meta_val":  ParagraphStyle("meta_val", parent=base["Normal"],
                                    textColor=INK_900, fontSize=11,
                                    leading=13, fontName="Helvetica-Bold"),
        "table_header": ParagraphStyle("th", parent=base["Normal"],
                                       textColor=CREAM, fontSize=9,
                                       leading=11, fontName="Helvetica-Bold"),
        "cell":      ParagraphStyle("cell", parent=base["Normal"],
                                    textColor=INK_900, fontSize=9, leading=11),
        "cell_num":  ParagraphStyle("cell_num", parent=base["Normal"],
                                    textColor=INK_900, fontSize=9, leading=11,
                                    alignment=TA_RIGHT,
                                    fontName="Helvetica"),
    }


class PDFReportBuilder:
    """
    High-level builder. Call `build(sections)` with a list of section dicts;
    get back a PDF as bytes ready to stream to the client.
    """

    def __init__(self, *, title, subtitle="", metadata=None,
                 company_name="PayBitnex", company_tagline="Global Payments"):
        self.title = title
        self.subtitle = subtitle
        self.metadata = metadata or {}
        self.company_name = company_name
        self.company_tagline = company_tagline
        self.styles = _build_styles()

    # ── Header / footer drawn on every page ──────────────────────────
    def _draw_header_footer(self, canv: canvas.Canvas, doc):
        width, height = A4
        canv.saveState()

        # ---- HEADER ---- thin branded band + company name
        canv.setFillColor(BRAND_DARK)
        canv.rect(0, height - 18 * mm, width, 18 * mm, fill=1, stroke=0)

        # Left: "PB" monogram tile in brand green
        canv.setFillColor(CREAM)
        canv.setFont("Helvetica-Bold", 12)
        canv.drawString(15 * mm, height - 11 * mm, self.company_name)
        canv.setFillColor(BRAND_LITE)
        canv.setFont("Helvetica", 8)
        canv.drawString(15 * mm, height - 15.5 * mm, self.company_tagline)

        # Right: small label
        canv.setFillColor(BRAND_LITE)
        canv.setFont("Helvetica", 8)
        label = "CLOSING REPORT"
        tw = canv.stringWidth(label, "Helvetica", 8)
        canv.drawString(width - 15 * mm - tw, height - 11 * mm, label)

        # ---- FOOTER ---- page number + generation timestamp
        canv.setFillColor(INK_500)
        canv.setFont("Helvetica", 7.5)
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        left_text = f"Generated {now}"
        canv.drawString(15 * mm, 12 * mm, left_text)

        page_num = canv.getPageNumber()
        right_text = f"Page {page_num}"
        tw = canv.stringWidth(right_text, "Helvetica", 7.5)
        canv.drawString(width - 15 * mm - tw, 12 * mm, right_text)

        # Thin rule above footer
        canv.setStrokeColor(INK_300)
        canv.setLineWidth(0.3)
        canv.line(15 * mm, 16 * mm, width - 15 * mm, 16 * mm)

        canv.restoreState()

    # ── Cover block (top of page 1) ──────────────────────────────────
    def _cover_block(self):
        flowables = []
        flowables.append(Paragraph(self.title, self.styles["title"]))
        if self.subtitle:
            flowables.append(Paragraph(self.subtitle, self.styles["subtitle"]))

        # Metadata pair-grid (2 per row)
        if self.metadata:
            items = list(self.metadata.items())
            # Chunk into rows of two
            rows = []
            for i in range(0, len(items), 2):
                pair = items[i:i + 2]
                left_key, left_val = pair[0]
                right_key, right_val = pair[1] if len(pair) > 1 else ("", "")
                rows.append([
                    Paragraph(str(left_key).upper(), self.styles["meta_key"]),
                    Paragraph(str(right_key).upper(), self.styles["meta_key"]),
                ])
                rows.append([
                    Paragraph(str(left_val), self.styles["meta_val"]),
                    Paragraph(str(right_val), self.styles["meta_val"]),
                ])
            t = Table(rows, colWidths=[3.4 * inch, 3.4 * inch])
            t.setStyle(TableStyle([
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
                ("TOPPADDING",    (0, 0), (-1, -1), 1),
                ("LEFTPADDING",   (0, 0), (-1, -1), 0),
                ("RIGHTPADDING",  (0, 0), (-1, -1), 0),
            ]))
            flowables.append(t)
            flowables.append(Spacer(1, 10))

        # Divider
        divider = Table([[""]], colWidths=[6.8 * inch], rowHeights=[0.5])
        divider.setStyle(TableStyle([
            ("LINEABOVE", (0, 0), (-1, 0), 0.8, BRAND_DARK),
        ]))
        flowables.append(divider)
        flowables.append(Spacer(1, 10))

        return flowables

    # ── Section renderers ────────────────────────────────────────────
    def _render_heading(self, text):
        return [Paragraph(text, self.styles["heading"])]

    def _render_paragraph(self, text):
        return [Paragraph(text, self.styles["body"])]

    def _render_kpi_grid(self, items):
        """
        Banking-style KPI tiles. items = [{"label": str, "value": str,
        "sub": str?}, ...]. Rendered as a 4-up grid.
        """
        if not items:
            return []

        # Build cells, 4 per row
        cells = []
        for it in items:
            cell_table = Table(
                [
                    [Paragraph(str(it.get("label", "")).upper(), self.styles["meta_key"])],
                    [Paragraph(str(it.get("value", "")), self.styles["meta_val"])],
                    [Paragraph(str(it.get("sub", "")), self.styles["small"])],
                ],
                colWidths=[1.55 * inch],
            )
            cell_table.setStyle(TableStyle([
                ("BACKGROUND",    (0, 0), (-1, -1), INK_100),
                ("LEFTPADDING",   (0, 0), (-1, -1), 8),
                ("RIGHTPADDING",  (0, 0), (-1, -1), 8),
                ("TOPPADDING",    (0, 0), (0, 0), 8),
                ("BOTTOMPADDING", (0, 2), (0, 2), 8),
            ]))
            cells.append(cell_table)

        # Chunk into rows of 4
        rows = []
        while cells:
            row, cells = cells[:4], cells[4:]
            # Pad so row always has 4 items for stable layout
            while len(row) < 4:
                row.append("")
            rows.append(row)

        outer = Table(rows, colWidths=[1.7 * inch] * 4)
        outer.setStyle(TableStyle([
            ("LEFTPADDING",   (0, 0), (-1, -1), 2),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 2),
            ("TOPPADDING",    (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ]))
        return [outer, Spacer(1, 10)]

    def _render_table(self, headers, rows, *, col_widths=None,
                      align=None, total_row=None):
        # Build data — headers as Paragraph so they wrap if long
        data = []
        if headers:
            data.append([
                Paragraph(str(h), self.styles["table_header"]) for h in headers
            ])

        for row in rows:
            data_row = []
            for val in row:
                data_row.append(Paragraph(str(val) if val is not None else "",
                                          self.styles["cell"]))
            data.append(data_row)

        if total_row:
            data.append([
                Paragraph(f"<b>{v}</b>" if v is not None else "",
                          self.styles["cell"])
                for v in total_row
            ])

        # Column widths default: split available 7 inches evenly
        ncols = len(headers) if headers else (len(rows[0]) if rows else 1)
        if col_widths:
            cws = [w * inch for w in col_widths]
        else:
            cws = [7.0 * inch / ncols] * ncols

        t = Table(data, colWidths=cws, repeatRows=1 if headers else 0)
        style = [
            ("BACKGROUND",   (0, 0), (-1, 0), BRAND_DARK),
            ("TEXTCOLOR",    (0, 0), (-1, 0), CREAM),
            ("FONTNAME",     (0, 0), (-1, 0), "Helvetica-Bold"),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
            ("TOPPADDING",    (0, 0), (-1, 0), 6),
            ("VALIGN",       (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING",  (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING",(0, 1), (-1, -1), 4),
            ("TOPPADDING",   (0, 1), (-1, -1), 4),
            ("LINEBELOW",    (0, 0), (-1, -1), 0.25, INK_300),
        ]
        # Per-column alignment
        if align:
            for i, a in enumerate(align):
                if a == "right":
                    style.append(("ALIGN", (i, 1), (i, -1), "RIGHT"))
                elif a == "center":
                    style.append(("ALIGN", (i, 1), (i, -1), "CENTER"))
        # Zebra striping on data rows
        for rix in range(1, len(data) - (1 if total_row else 0)):
            if rix % 2 == 0:
                style.append(("BACKGROUND", (0, rix), (-1, rix), INK_100))
        # Total row emphasis
        if total_row:
            style.append(("BACKGROUND",  (0, -1), (-1, -1), BRAND_LITE))
            style.append(("FONTNAME",    (0, -1), (-1, -1), "Helvetica-Bold"))
            style.append(("LINEABOVE",   (0, -1), (-1, -1), 0.8, BRAND_DARK))
            style.append(("TOPPADDING",  (0, -1), (-1, -1), 6))
            style.append(("BOTTOMPADDING", (0, -1), (-1, -1), 6))

        t.setStyle(TableStyle(style))
        return [t, Spacer(1, 8)]

    def _render_section(self, section):
        kind = section.get("type")
        if kind == "heading":
            return self._render_heading(section["text"])
        if kind == "paragraph":
            return self._render_paragraph(section["text"])
        if kind == "kpi_grid":
            return self._render_kpi_grid(section["items"])
        if kind == "table":
            return self._render_table(
                section.get("headers", []),
                section.get("rows", []),
                col_widths=section.get("col_widths"),
                align=section.get("align"),
                total_row=section.get("total_row"),
            )
        if kind == "spacer":
            return [Spacer(1, section.get("height", 8))]
        if kind == "page_break":
            return [PageBreak()]
        return []

    # ── Entry point ──────────────────────────────────────────────────
    def build(self, sections):
        buf = BytesIO()
        doc = BaseDocTemplate(
            buf, pagesize=A4,
            leftMargin=15 * mm, rightMargin=15 * mm,
            topMargin=25 * mm, bottomMargin=20 * mm,
            title=self.title,
        )
        frame = Frame(
            doc.leftMargin, doc.bottomMargin,
            doc.width, doc.height,
            leftPadding=0, rightPadding=0,
            topPadding=0, bottomPadding=0,
            showBoundary=0,
        )
        template = PageTemplate(
            id="main", frames=[frame],
            onPage=self._draw_header_footer,
        )
        doc.addPageTemplates([template])

        # Build flowables
        story = self._cover_block()
        for s in sections:
            story.extend(self._render_section(s))

        doc.build(story)
        return buf.getvalue()
