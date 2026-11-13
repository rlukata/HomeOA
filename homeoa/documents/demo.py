"""Demo documents for seed_demo.

The titles, areas and visibility follow the prototype. Each file is a short PDF of made-up
text, so search finds words inside it. The declaration's 1994 page is a scan (a picture with
no text layer), which only OCR can read.
"""

from __future__ import annotations

import datetime as dt

from .models import Category, GoverningKind, Visibility

DOCS = [
    (
        "Declaration (1994, amended 2011)",
        Category.GOVERNING,
        Visibility.TENANTS,
        GoverningKind.DECLARATION,
        dt.date(2011, 3, 1),
    ),
    ("Bylaws", Category.GOVERNING, Visibility.TENANTS, GoverningKind.BYLAWS, dt.date(2011, 3, 1)),
    (
        "Rules & regulations 2025",
        Category.GOVERNING,
        Visibility.TENANTS,
        GoverningKind.RULES,
        dt.date(2025, 1, 1),
    ),
    (
        "Fine schedule (adopted Feb 2025)",
        Category.GOVERNING,
        Visibility.TENANTS,
        GoverningKind.RESOLUTION,
        dt.date(2025, 2, 1),
    ),
    ("Budget 2026", Category.FINANCIAL, Visibility.OWNERS, "", None),
    ("Financial statements · September 2026", Category.FINANCIAL, Visibility.OWNERS, "", None),
    ("Board minutes · Sep 17, 2026", Category.MINUTES, Visibility.OWNERS, "", None),
    ("Executive session notes · Sep 17, 2026", Category.MINUTES, Visibility.BOARD, "", None),
    ("Master insurance certificate 2026", Category.INSURANCE, Visibility.OWNERS, "", None),
    ("Greenway Landscaping contract", Category.CONTRACTS, Visibility.BOARD, "", None),
]


# The demo welcome packet: the governing documents, fine schedule and budget.
WELCOME = {
    "Declaration (1994, amended 2011)",
    "Bylaws",
    "Rules & regulations 2025",
    "Fine schedule (adopted Feb 2025)",
    "Budget 2026",
}
WELCOME_MESSAGE = (
    "Welcome to {name}! We're a small, self-managed association: the board are your "
    "neighbors, volunteering their time.\n\n"
    "Please read the declaration, bylaws and rules below. Dues are due on the 10th of each "
    "month, and the easiest way to pay is autopay by bank transfer."
)


def _texts(association) -> dict[str, tuple[list[str], list[str]]]:
    """Each document's made-up text: {title: (typed lines, scanned lines)}."""
    from homeoa.core.demo import budget_lines

    name = association.name
    budget = [
        f"{line}: ${amount:,.2f}" for line, amount in budget_lines()["operating_expense"] if amount
    ]
    return {
        "Declaration (1994, amended 2011)": (
            [
                "First amendment (2011)",
                "Article 9 is amended: dogs are kept on a leash in the common areas.",
                "Article 12 is added: each unit owns the parking space assigned to it.",
            ],
            [
                "DECLARATION OF CONDOMINIUM",
                f"{name}, a condominium",
                "Recorded 1994. All names in this sample are made up.",
                "Article 4. Units. There are nine units, lettered A to I.",
                "Article 7. Common expenses are assessed by allocated interest.",
                "Article 9. Pets. Each unit may keep two household pets.",
                "Article 11. Leasing. A unit is leased for 30 days or more.",
            ],
        ),
        "Bylaws": (
            [
                "Article II. The owners elect the board at the annual meeting.",
                "Directors serve two-year terms.",
                "Article III. Officers: president, vice president, secretary and treasurer.",
                "The treasurer keeps the books and reports to the board each month.",
                "Article V. The fiscal year starts on January 1.",
            ],
            [],
        ),
        "Rules & regulations 2025": (
            [
                "Quiet hours are 10 pm to 7 am.",
                "Garbage and recycling carts go back inside within a day of pickup.",
                "No grills on balconies; use the shared grill by the garden.",
                "Tell the board three days before a move-in or move-out.",
                "Pets are leashed in the common areas; owners clean up after them.",
                "Park in your own space; guests park on the street.",
            ],
            [],
        ),
        "Fine schedule (adopted Feb 2025)": (
            [
                "First violation of a rule: a written warning, no fine.",
                "Second violation of the same rule within a year: $50.",
                "Each further violation within a year: $100.",
                "An owner may ask for a hearing before a fine is charged.",
            ],
            [],
        ),
        "Budget 2026": (["Operating expenses for 2026:", *budget], []),
        "Financial statements · September 2026": (
            [
                "Balance sheet and income statement for September 2026.",
                "Prepared by the treasurer from the association's books.",
                "Operating cash, reserve savings and the insurance fund are shown separately.",
            ],
            [],
        ),
        "Board minutes · Sep 17, 2026": (
            [
                "Called to order at 7:00 pm; all directors present.",
                "The treasurer's report was accepted.",
                "The board reviewed the fall gutter cleaning schedule.",
                "Owners asked about the roof; the board will get two roof inspection quotes.",
                "Adjourned at 8:10 pm.",
            ],
            [],
        ),
        "Executive session notes · Sep 17, 2026": (
            [
                "One account past due: the board offered a payment plan.",
                "The board discussed the attorney's advice on the collection policy.",
            ],
            [],
        ),
        # The same policy as insurance.demo.
        "Master insurance certificate 2026": (
            [
                "Certificate of insurance (sample)",
                f"Named insured: {name}",
                "Carrier: Evergreen Mutual (demo), policy EM-CR-2026",
                "Policy period: March 1, 2026 to March 1, 2027",
                "Property and general liability; liability limit $2,000,000.",
                "Deductible: $10,000 per loss.",
            ],
            [],
        ),
        "Greenway Landscaping contract": (
            [
                f"Agreement between {name} and Greenway Landscaping.",
                "Mowing, edging and leaf cleanup from March to November.",
                "Either party may end this agreement with 30 days' written notice.",
            ],
            [],
        ),
    }


def pdf(title: str, lines: list[str], scanned: list[str] | None = None) -> bytes:
    """A small PDF: ``title`` and ``lines`` as typed text, after a first page drawn as a
    picture of ``scanned`` with no text layer, like an office scan."""
    import io

    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.utils import ImageReader, simpleSplit
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=LETTER)
    c.setTitle(title)
    if scanned:
        c.drawImage(ImageReader(_scan(scanned)), 0, 0, *LETTER)
        c.showPage()
    c.setFont("Helvetica-Bold", 14)
    c.drawString(72, 720, title)
    c.setFont("Helvetica", 11)
    y = 692
    for line in lines:
        for part in simpleSplit(line, "Helvetica", 11, LETTER[0] - 144):
            c.drawString(72, y, part)
            y -= 16
    c.showPage()
    c.save()
    return buf.getvalue()


def _scan(lines: list[str]):
    """A letter page at 200 dpi, slightly crooked and soft, as a scanner makes it."""
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    page = Image.new("L", (1700, 2200), 250)
    draw = ImageDraw.Draw(page)
    font = ImageFont.load_default(40)
    y = 200
    for line in lines:
        draw.text((150, y), line, fill=25, font=font)
        y += 80
    page = page.rotate(0.6, fillcolor=245, resample=Image.Resampling.BICUBIC)
    return page.filter(ImageFilter.GaussianBlur(0.6))


def seed(association) -> None:
    from . import welcome
    from .services import attach

    texts = _texts(association)
    for title, category, visibility, kind, effective in DOCS:
        lines, scanned = texts[title]
        version = attach(
            association,
            filename="document.pdf",
            content=pdf(title, lines, scanned),
            title=title,
            category=category,
            visibility=visibility,
            governing_kind=kind,
            effective_date=effective,
        )
        if title in WELCOME:
            welcome.set_in_packet(version.document, True)
    welcome.set_message(association, WELCOME_MESSAGE.format(name=association.name))
