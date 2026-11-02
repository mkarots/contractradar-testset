"""Tables track: synthetic documents whose TABLES break text-dump extraction.

Built from the failure modes Jmuz named in the pilot thread: tables without set boundaries in statements, grouped ID
rows such as dates, and page boundaries (Ash: "a table crossing a page would be a useful one"). Each case comes from
one row list, so the PDF and expected.csv cannot disagree:

  tables/TB-00N/document.pdf      the document (every page marked as a synthetic test document)
  tables/TB-00N/expected.csv      every table row once, values normalised: ISO dates, plain numbers, blanks empty
  tables/TB-00N/spec.json         columns (name, printed header, type), hazards, what is and is not a row
  tables/TB-00N/notes.md          why the case is hard (the naive-extraction section is added by extract_tables_naive)
  tables/TB-00N/pages/page-N.png and text.txt (pdftotext -layout)

Run:  python -m crtest.gen_tables --out tables
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import shutil
import subprocess

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Flowable, PageBreak, Paragraph, Spacer, Table, TableStyle

from .sysdeps import require_tools

from . import corpus
from .render import MARGIN, PAGE_W, P, STYLES, build_pdf, esc

GENERATOR_VERSION = "0.2.0"   # 0.2.0: TB-003 split laid out by hand (one header per page), TB-004 week totals undated
TEXT_WIDTH = PAGE_W - 2 * MARGIN
CELL = STYLES["cell"]
CELLB = STYLES["cellb"]
CELLR = ParagraphStyle("cellr", parent=CELL, alignment=TA_RIGHT)
CELLBR = ParagraphStyle("cellbr", parent=CELLB, alignment=TA_RIGHT)
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
# Printed column widths in mm, left to right (TB-004: the printed columns, not the derived week_ending). Every table is
# centred in the text frame, so these fix each printed column's x position; crtest.extract_tables_tools maps tool output
# onto the printed columns with them.
COLUMN_WIDTHS_MM = {
    "TB-001": [20, 22, 64, 21, 21, 24],
    "TB-002": [22, 23, 44, 36, 23, 24],
    "TB-003": [11, 38, 76, 22, 27],
    "TB-004": [20, 72, 21, 20, 18, 21],
}

HAZARDS = {
    "no_ruling_lines": "No ruled lines or cell borders: column boundaries have to be inferred from alignment alone.",
    "wrapped_cells": "Long cell text wraps onto two or more lines; the continuation lines are not new rows.",
    "empty_cells": "Some cells are blank (a debit or a credit); splitting a line on whitespace shifts the next value into the wrong column.",
    "negative_in_brackets": "A negative amount is printed in brackets, (146.10), not with a minus sign.",
    "brought_forward_row": "The statement opens with a 'balance brought forward' line, which is a row of the statement.",
    "totals_row": "A totals line closes the table; it is not a row.",
    "suppressed_repeats": "Repeated values (date, directorate, supplier) are printed only on the first row of each run; the rows below inherit them.",
    "identical_rows": "Two rows are identical apart from the transaction number; both are real and both must be kept.",
    "group_header_rows": "A date is printed as a full-width heading above its group; it is a value for every row below it, not a row of its own.",
    "subtotal_rows": "Group subtotal lines are not rows.",
    "table_crosses_page": "The table runs over a page break.",
    "split_row": "One row is split by the page break: its text continues on the next page, under the repeated header, with the other cells empty.",
    "repeated_header": "The header row is printed again on the next page; it is not a data row.",
    "footer_interleaves": "Page footer and running-header text fall between table rows in a text dump.",
    "carried_forward_rows": "'Carried forward' and 'brought forward' subtotal lines at the page break are not rows.",
    "group_crosses_page": "A date group starts on one page and continues on the next without the date being printed again.",
}


def uk(d: dt.date) -> str:
    return d.strftime("%d/%m/%Y")


def long_date(d: dt.date, weekday: bool = False) -> str:
    s = f"{d.day} {MONTHS[d.month - 1]} {d.year}"
    return f"{d.strftime('%A')} {s}" if weekday else s


def money(v: float | None, brackets: bool = False, pound: bool = False) -> str:
    if v is None:
        return ""
    s = f"{abs(v):,.2f}"
    if pound:
        s = "£" + s
    if v < 0:
        return f"({s})" if brackets else f"-{s}"
    return s


def num(v: float | None) -> str:
    return "" if v is None else f"{v:.2f}"


def M(markup: str, style: str = "body") -> Paragraph:
    """A paragraph from reportlab markup (render.P escapes its input, so it cannot take <b> or <br/>)."""
    return Paragraph(markup, STYLES[style])


def bold_row(values: list[str], right: tuple[int, ...]) -> list:
    return [cell(v, CELLBR if i in right else CELLB) for i, v in enumerate(values)]


def cell(text: str, style: ParagraphStyle = CELL):
    return Paragraph(esc(text), style) if text else ""


def table(header: list[str], body: list[list], widths: list[float], right: tuple[int, ...] = (), *, grid: bool = False,
          header_rule: bool = False, extra_style: list | None = None, repeat_header: bool = True) -> Table:
    data = [[cell(h, CELLBR if i in right else CELLB) for i, h in enumerate(header)]]
    for r in body:
        data.append([x if not isinstance(x, str) else cell(x, CELLR if i in right else CELL) for i, x in enumerate(r)])
    t = Table(data, colWidths=widths, repeatRows=1 if repeat_header else 0)
    style = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 2.2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.2),
             ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3)]
    if grid:
        style += [("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#8c8c8c")), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dfe6ef"))]
    elif header_rule:
        style.append(("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.HexColor("#333333")))
    t.setStyle(TableStyle(style + (extra_style or [])))
    return t


def cell_lines(text: str, width: float, style: ParagraphStyle = CELL) -> list[str]:
    """The lines a table cell of this inner width wraps text into."""
    para = Paragraph(esc(text), style)
    para.wrap(width, 10_000)
    return [" ".join(words) for _extra, words in para.blPara.lines]


class AtFrameFoot(Flowable):
    """Draws a flowable at the foot of the frame, the space above it left blank (ReportLab's TopPadder, but it keeps the
    blank height in .pad so the generator can check the layout). It never splits: if it does not fit, it moves on."""

    def __init__(self, flowable: Flowable):
        super().__init__()
        self.flowable, self.pad = flowable, 0.0
        self.hAlign = getattr(flowable, "hAlign", "CENTER")

    def wrap(self, aW, aH):
        w, h = self.flowable.wrap(aW, aH)
        self.pad, self.width, self.height = aH - h, w, h
        return w, h

    def split(self, aW, aH):
        return []

    def drawOn(self, canvas, x, y, _sW=0):
        self.flowable.drawOn(canvas, x, y - max(0.0, self.pad), _sW)


def pdftext(path: str, page: int | None = None) -> str:
    args = ["pdftotext", "-layout"] + (["-f", str(page), "-l", str(page)] if page else []) + [path, "-"]
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def page_count(path: str) -> int:
    from pypdf import PdfReader
    return len(PdfReader(path).pages)


# ---------------------------------------------------------------- TB-001
def tb001(path: str) -> dict:
    """Supplier statement of account with no ruled lines, wrapped details, blank debit/credit cells, a bracketed negative."""
    council, supplier = corpus.COUNCILS[0], corpus.SUPPLIERS[0]
    opening = 12480.00
    tx = [
        (dt.date(2026, 10, 2), "INV-40871", "Planned preventative maintenance, Civic Centre passenger lifts: quarterly service visit and LOLER thorough examination certificate", 3240.00, None),
        (dt.date(2026, 10, 6), "PAY-77812", "BACS payment received, thank you", None, 12480.00),
        (dt.date(2026, 10, 9), "INV-40902", "Reactive repair: replacement circulating pump on boiler 2, Market Walk depot", 1186.50, None),
        (dt.date(2026, 10, 12), "CN-2207", "Credit note against INV-40755 (duplicate out-of-hours call-out charge)", None, 245.00),
        (dt.date(2026, 10, 15), "INV-40933", "Car park lighting: replace 6 failed LED lanterns, columns 4 to 9", 812.40, None),
        (dt.date(2026, 10, 19), "INV-40951", "Planned preventative maintenance, Riverside offices: fire alarm and emergency lighting tests", 2460.00, None),
        (dt.date(2026, 10, 21), "ADJ-118", "Service credit: KPI 3 (emergency response within 2 hours) missed in Q2", None, 1600.00),
        (dt.date(2026, 10, 23), "PAY-78044", "BACS payment received", None, 6000.00),
        (dt.date(2026, 10, 27), "INV-40988", "Emergency drainage works, Riverside car park: out of hours, 2 operatives for 6 hours, jetting and CCTV survey", 2915.25, None),
        (dt.date(2026, 10, 30), "INV-41002", "Consumables and parts as per attached schedule", 96.80, None),
    ]
    rows, bal = [], opening
    rows.append({"date": dt.date(2026, 10, 1), "reference": "", "details": "Balance brought forward", "debit_gbp": None, "credit_gbp": None, "balance_gbp": opening})
    for d, ref, det, dr, cr in tx:
        bal = round(bal + (dr or 0) - (cr or 0), 2)
        rows.append({"date": d, "reference": ref, "details": det, "debit_gbp": dr, "credit_gbp": cr, "balance_gbp": bal})
    tot_dr = round(sum(r["debit_gbp"] or 0 for r in rows), 2)
    tot_cr = round(sum(r["credit_gbp"] or 0 for r in rows), 2)
    assert any(r["balance_gbp"] < 0 for r in rows) and round(opening + tot_dr - tot_cr, 2) == bal
    header = ["Date", "Ref", "Details", "Debit", "Credit", "Balance"]
    widths = [w * mm for w in COLUMN_WIDTHS_MM["TB-001"]]
    body = [[uk(r["date"]), r["reference"], r["details"], money(r["debit_gbp"]), money(r["credit_gbp"]), money(r["balance_gbp"], brackets=True)] for r in rows]
    body.append(bold_row(["", "", "Totals", money(tot_dr), money(tot_cr), ""], (3, 4, 5)))
    t = table(header, body, widths, right=(3, 4, 5))
    story = [
        P(supplier["name"], "title"),
        M(esc(supplier["address"]) + "<br/>Company number " + supplier["reg"], "small"),
        Spacer(1, 5 * mm),
        P("Statement of account", "h2"),
        M(f"To: Accounts Payable, {esc(council['name'])}, {esc(council['address'])}<br/>Account: {council['short']}/FM/2291 &nbsp;&nbsp; "
          f"Statement date: 31 October 2026 &nbsp;&nbsp; Contract: Facilities management services (lot 2)", "body"),
        Spacer(1, 3 * mm), t, Spacer(1, 5 * mm),
        M(f"<b>Balance now due: {money(bal, pound=True)}</b>", "body"),
        P("Amounts are in pounds sterling. Bracketed balances are in the Council's favour. Payment terms: 30 days from the invoice date. "
          "Please quote the invoice reference with any remittance.", "small"),
    ]
    build_pdf(path, story, supplier["name"], "Statement of account", header=f"{supplier['name']} · Statement of account · {council['short']}/FM/2291")
    return {
        "case_id": "TB-001", "slug": "statement-no-ruling",
        "title": "Supplier statement of account with no ruled lines",
        "columns": [("date", "Date", "date"), ("reference", "Ref", "id"), ("details", "Details", "text"),
                    ("debit_gbp", "Debit", "money"), ("credit_gbp", "Credit", "money"), ("balance_gbp", "Balance", "money")],
        "rows": rows,
        "hazards": ["no_ruling_lines", "wrapped_cells", "empty_cells", "negative_in_brackets", "brought_forward_row", "totals_row"],
        "row_rules": "Every statement line is a row, including the opening balance brought forward. The Totals line is not a row. "
                     "Bracketed amounts are negative. A blank debit or credit cell stays blank.",
        "why": "The statement has no lines between columns or rows. Details wrap onto a second or third line, every row has "
               "either the Debit or the Credit cell empty, and the balance goes negative once, printed in brackets. A "
               "text dump split on whitespace turns each wrapped line into a new row and slides credits into the Debit column.",
        "naive_stop": r"^\s*Totals\b",
    }


# ---------------------------------------------------------------- TB-002
def tb002(path: str) -> dict:
    """Council spend statement: date, directorate and supplier printed only on the first row of each run."""
    council = corpus.COUNCILS[1]
    S = {s["name"].split()[0]: s["name"] for s in corpus.SUPPLIERS}
    data = [
        (dt.date(2026, 10, 6), [
            ("Environment", S["Brightwater"], "Waste collection contract", "5100231", 84612.00),
            ("Environment", S["Fenwick"], "Grounds maintenance", "5100232", 6240.50),
            ("Environment", S["Fenwick"], "Tree works", "5100233", 1180.00),
            ("Resources", S["Elmstead"], "Software licences", "5100234", 12950.00),
            ("Resources", S["Elmstead"], "Hardware maintenance", "5100235", 2310.75),
        ]),
        (dt.date(2026, 10, 13), [
            ("Housing", S["Dunmore"], "Temporary accommodation", "5100298", 3465.00),
            ("Housing", S["Dunmore"], "Temporary accommodation", "5100299", 3465.00),
            ("Housing", S["Alderman"], "Void property repairs", "5100300", 2775.20),
            ("Leisure", S["Halbrook"], "Management fee", "5100301", 41666.67),
        ]),
        (dt.date(2026, 10, 20), [
            ("Environment", S["Cresswell"], "Highways maintenance", "5100355", 18902.40),
            ("Environment", S["Brightwater"], "Refund: overpaid gate fees", "5100356", -1250.00),
            ("Resources", S["Greystoke"], "Capital works: depot roof", "5100357", 96400.00),
        ]),
        (dt.date(2026, 10, 27), [
            ("Resources", S["Alderman"], "Facilities management", "5100410", 23118.00),
            ("Resources", S["Elmstead"], "Telephony", "5100411", 845.00),
            ("Leisure", S["Halbrook"], "Utilities recharge", "5100412", 7302.18),
            ("Leisure", S["Fenwick"], "Sports pitch renovation", "5100413", 4980.00),
        ]),
    ]
    rows, body, style = [], [], []
    for d, items in data:
        prev_dir = prev_sup = None
        for i, (direc, sup, exp, txn, amt) in enumerate(items):
            rows.append({"payment_date": d, "directorate": direc, "supplier": sup, "expense_type": exp, "transaction_no": txn, "amount_gbp": amt})
            show_dir = direc != prev_dir
            show_sup = show_dir or sup != prev_sup
            body.append([uk(d) if i == 0 else "", direc if show_dir else "", sup if show_sup else "", exp, txn, money(amt)])
            prev_dir, prev_sup = direc, sup
        style.append(("LINEBELOW", (0, len(body)), (-1, len(body)), 0.3, colors.HexColor("#9a9a9a")))
    total = round(sum(r["amount_gbp"] for r in rows), 2)
    header = ["Payment date", "Directorate", "Supplier", "Expense type", "Transaction no.", "Amount (£)"]
    widths = [w * mm for w in COLUMN_WIDTHS_MM["TB-002"]]
    t = table(header, body, widths, right=(5,), header_rule=True, extra_style=style)
    story = [
        P(council["name"], "title"),
        P("Expenditure over £500: October 2026", "subtitle"),
        P("Payments to suppliers of £500 or more, published monthly under the Local Government Transparency Code. Amounts exclude "
          "recoverable VAT.", "body"),
        Spacer(1, 3 * mm), t, Spacer(1, 4 * mm),
        M(f"<b>Total for October 2026: {money(total, pound=True)}</b> ({len(rows)} transactions)", "body"),
        P("Negative amounts are refunds or credits received.", "small"),
    ]
    build_pdf(path, story, council["name"], "Expenditure over £500 - October 2026", header=f"{council['name']} · Transparency Code publication")
    return {
        "case_id": "TB-002", "slug": "spend-grouped-dates",
        "title": "Council spend statement with grouped dates, directorates and suppliers",
        "columns": [("payment_date", "Payment date", "date"), ("directorate", "Directorate", "text"), ("supplier", "Supplier", "text"),
                    ("expense_type", "Expense type", "text"), ("transaction_no", "Transaction no.", "id"), ("amount_gbp", "Amount (£)", "money")],
        "rows": rows,
        "hazards": ["suppressed_repeats", "identical_rows", "wrapped_cells", "totals_row"],
        "row_rules": "One row per transaction. A blank Payment date, Directorate or Supplier cell repeats the value above it, "
                     "within the same date group. Negative amounts are refunds and stay negative.",
        "why": "The payment date is printed once per date group, the directorate once per run within a date, and the supplier once "
               "per run within a directorate, so most rows show only some of their values. Two Dunmore rows are identical apart "
               "from the transaction number. Long supplier names wrap onto a second line.",
        "naive_stop": r"^\s*Total for\b",
    }


# ---------------------------------------------------------------- TB-003
def tb003(path: str) -> dict:
    """Ruled payment-milestone schedule that crosses a page, with one row split by the page break."""
    council, supplier = corpus.COUNCILS[2], next(s for s in corpus.SUPPLIERS if s["name"].startswith("Greystoke"))
    ms = [
        ("Mobilisation and site set-up", "Site compound established and welfare facilities accepted by the Contract Administrator", dt.date(2027, 1, 12), 48500.00),
        ("Enabling works", "Asbestos removal certificate issued and demolition of the former salt barn complete", dt.date(2027, 2, 26), 132750.00),
        ("Substructure", "Foundations and ground-bearing slab signed off by the Council's structural engineer", dt.date(2027, 4, 30), 286400.00),
        ("Frame", "Steel frame erected, plumbed and bolted, with the frame inspection certificate issued", dt.date(2027, 6, 18), 412900.00),
        ("Envelope", "Roof covering, wall cladding, glazing and the vehicle bay doors complete so that the building is weathertight, "
                     "confirmed in writing by the Contract Administrator after a water test of every roof outlet and gutter and a "
                     "visual inspection of all flashings, sealant joints and door seals", dt.date(2027, 8, 27), 365200.00),
        ("Mechanical and electrical first fix", "Containment, cabling and pipework installed and pressure-tested", dt.date(2027, 10, 22), 241300.00),
        ("Vehicle wash bay and interceptor", "Wash bay, oil interceptor and discharge consent in place", dt.date(2027, 11, 26), 98650.00),
        ("Internal finishes", "Floors, partitions, ceilings and decorations complete in all rooms", dt.date(2028, 1, 21), 154800.00),
        ("External works and drainage", "Yard surfacing, drainage connections and site lighting complete", dt.date(2028, 3, 3), 176250.00),
        ("Commissioning", "All plant commissioned and the operation and maintenance manuals accepted", dt.date(2028, 4, 14), 64900.00),
        ("Practical completion", "Certificate of practical completion issued", dt.date(2028, 5, 5), 120000.00),
        ("End of defects period", "Certificate of making good defects issued, 12 months after practical completion", dt.date(2029, 5, 5), 50000.00),
    ]
    split_idx, split_lines = 4, 2   # milestone 5 (Envelope) is cut by the page break after 2 lines of its trigger
    rows = [{"milestone_no": str(i), "milestone": m, "payment_trigger": trig, "due_date": d, "amount_gbp": amt}
            for i, (m, trig, d, amt) in enumerate(ms, 1)]
    total = round(sum(r["amount_gbp"] for r in rows), 2)
    header = ["No.", "Milestone", "Payment trigger", "Due date", "Amount (£, ex VAT)"]
    widths = [w * mm for w in COLUMN_WIDTHS_MM["TB-003"]]
    body = [[r["milestone_no"], r["milestone"], r["payment_trigger"], uk(r["due_date"]), money(r["amount_gbp"])] for r in rows]
    clauses = [
        "1. This Schedule sets out the milestones at which the Contractor may apply for payment under clause 4 of the Contract. "
        "Each application shall be accompanied by the evidence named in the Payment trigger column.",
        "2. The Contract Administrator shall certify each milestone within 10 working days of a complete application. The Council "
        "shall pay a certified amount within 30 days of receiving a valid invoice, in accordance with section 68 of the "
        "Procurement Act 2023 and the Contract.",
        "3. Retention of 3 per cent applies to milestones 1 to 10. Half of the retention is released at practical completion and "
        "the balance at the end of the defects period.",
        "4. Amounts exclude VAT. Milestones may be re-sequenced only by a variation instructed in writing.",
        "5. An application shall be made on the Council's application form and shall state the milestone number, the sum applied "
        "for and the evidence enclosed. An application without the evidence named in the Payment trigger column is not a "
        "complete application, and the Contract Administrator shall say so in writing within 5 working days.",
        "6. The due date for payment of a certified milestone is the date on which the Council receives a valid invoice for it. "
        "The final date for payment is 30 days after the due date.",
        "7. The Council shall issue a payment notice within 5 days of the due date, stating the sum it considers due and how that "
        "sum is calculated. If the Council intends to pay less than the notified sum, it shall issue a pay less notice no later "
        "than 7 days before the final date for payment, giving its reasons.",
        "8. If a certified amount is not paid by the final date for payment, simple interest is payable on it at 8 per cent a "
        "year above the Bank of England base rate from the final date for payment until the date of payment.",
        "9. Invoices shall be addressed to the Council's Accounts Payable team, quote the contract reference and the milestone "
        "number, and be submitted through the Council's e-invoicing portal. Paper invoices are not accepted.",
        "10. Payment of a milestone is not evidence that the work is complete or free from defects and does not affect the "
        "Contract Administrator's powers under clause 17 of the Contract.",
        "11. Where a milestone is achieved in part only, the Contract Administrator may certify a part payment in proportion to "
        "the value of the work completed, on the Contractor's written request.",
        "12. The Council may deduct from a payment under this Schedule any sum the Contractor owes it under the Contract, provided "
        "the deduction is stated in its payment notice or pay less notice.",
        "13. The Contractor shall not assign the right to receive payment under this Schedule without the Council's prior written "
        "consent, which shall not be unreasonably withheld.",
        "14. The amounts in the table below are fixed. They are not subject to fluctuation, indexation or any other adjustment "
        "except by a variation under clause 22 of the Contract.",
    ]
    # ReportLab's own in-row split (Table splitInRow) draws the header twice on page 1 - it first splits after milestone 4,
    # then splits what is left again inside milestone 5, repeated header and all - and leaves the cut row open at the foot
    # of the page, which also hides that half of the row from ruling-line extractors. So the split is laid out by hand, the
    # way a word processor draws a row allowed to break across pages: page 1 ends with milestone 5's number, name, date,
    # amount and the first lines of its trigger, boxed, at the foot of the page; page 2 starts with the repeated header and
    # the rest of the trigger in a row whose other cells are empty.
    lines = cell_lines(rows[split_idx]["payment_trigger"], widths[2] - 6)
    if len(lines) <= split_lines:
        raise RuntimeError("TB-003: the split row's payment trigger is too short to split")
    top = body[split_idx][:2] + [" ".join(lines[:split_lines])] + body[split_idx][3:]
    rest = ["", "", " ".join(lines[split_lines:]), "", ""]
    total_row = bold_row(["", "Total", "", "", money(total)], (4,))
    part1 = table(header, body[:split_idx] + [top], widths, right=(4,), grid=True, repeat_header=False)
    part2 = table(header, [rest] + body[split_idx + 1:] + [total_row], widths, right=(4,), grid=True, repeat_header=False)
    foot = AtFrameFoot(part1)
    story = [P(council["name"], "title"),
             P("Contract for the redevelopment of the Market Walk highways depot", "subtitle"),
             P(f"Between {council['name']} (the Council) and {supplier['name']} (the Contractor). Contract reference "
               f"{council['short']}/CAP/0412.", "body"),
             P("Schedule 2: Payment milestones", "h2")]
    story += [P(c, "body") for c in clauses]
    story += [foot, PageBreak(), part2, Spacer(1, 4 * mm),
              M("Signed for the Council: ______________________ &nbsp;&nbsp; Signed for the Contractor: ______________________", "small")]
    build_pdf(path, story, council["name"], "Schedule 2 - Payment milestones", header=f"{council['name']} · {council['short']}/CAP/0412 · Schedule 2")
    p1, p2 = (" ".join(pdftext(path, n).split()) if page_count(path) >= n else "" for n in (1, 2))
    if (page_count(path) != 2 or lines[split_lines - 1] not in p1 or lines[split_lines] in p1 or lines[split_lines] not in p2
            or p1.count("Due date") != 1 or p2.count("Due date") != 1):
        raise RuntimeError("TB-003: the page break is not inside milestone 5, or the header is not printed once per page")
    if not 0 <= foot.pad <= 20 * mm:
        raise RuntimeError(f"TB-003: {foot.pad / mm:.0f} mm left blank above the table; adjust the clauses")
    return {
        "case_id": "TB-003", "slug": "milestones-split-row",
        "title": "Payment milestone schedule crossing a page, one row split by the page break",
        "columns": [("milestone_no", "No.", "id"), ("milestone", "Milestone", "text"), ("payment_trigger", "Payment trigger", "text"),
                    ("due_date", "Due date", "date"), ("amount_gbp", "Amount (£, ex VAT)", "money")],
        "rows": rows,
        "hazards": ["table_crosses_page", "split_row", "repeated_header", "footer_interleaves", "wrapped_cells", "totals_row"],
        "row_rules": "One row per milestone. Milestone 5's payment trigger starts at the foot of page 1 and finishes at the top of "
                     "page 2: it is one cell. The repeated header and the Total line are not rows.",
        "why": f"The schedule starts low on page 1, so milestone {split_idx + 1} (Envelope) is split by the page break: the first lines "
               "of its payment trigger are on page 1 and the rest continue on page 2, under the repeated header, in a row whose "
               "other cells are empty. The running header and the page footer fall between the two halves in a text dump.",
        "naive_stop": r"^\s*Total\b",
        "layout": {"blank_above_table_mm": round(foot.pad / mm, 1)},
    }


# ---------------------------------------------------------------- TB-004
def tb004(path: str) -> dict:
    """Borderless weekly payments statement: date group headings, subtotals, carried/brought forward lines, a group across the page."""
    council = corpus.COUNCILS[3]
    supplier = next(s for s in corpus.SUPPLIERS if s["name"].startswith("Cresswell"))
    weeks = [dt.date(2026, 7, 3) + dt.timedelta(days=7 * i) for i in range(4)]
    assert all(w.weekday() == 4 for w in weeks)
    orders = [
        [("HW-7101", "Pothole repairs, Victoria Square and Market Street (14 no.)", "HWY-310", 8420.00, 0.2),
         ("HW-7102", "Gully emptying, Ward 3 cyclic programme", "HWY-325", 3150.00, 0.2),
         ("HW-7103", "Road closure notices and permit fees, Canal Street resurfacing", "HWY-301", 640.00, 0.0)],
        [("HW-7118", "Carriageway resurfacing, Canal Street (inlay 40mm, 1,180 m2), including traffic management", "HWY-340", 46210.00, 0.2),
         ("HW-7119", "Replacement of damaged safety barrier, Northam Road bridge approach", "HWY-355", 5875.50, 0.2),
         ("HW-7120", "Out-of-hours emergency call-out: fallen tree, Fenwick Lane", "HWY-390", 1290.00, 0.2),
         ("HW-7121", "Street lighting: column replacement (3 no.), Chapel Street", "LGT-120", 7340.00, 0.2)],
        [("HW-7140", "Footway reconstruction, Bridge Street (both sides), including dropped kerbs at four crossings", "HWY-345", 31975.00, 0.2),
         ("HW-7141", "Line painting refresh, school keep-clear markings (6 schools)", "HWY-360", 2210.00, 0.2),
         ("HW-7142", "Gully emptying, Ward 4 cyclic programme", "HWY-325", 3150.00, 0.2),
         ("HW-7143", "Drainage CCTV survey and jetting, Riverside culvert", "HWY-330", 4480.00, 0.2),
         ("HW-7144", "Permit fees, Bridge Street works", "HWY-301", 410.00, 0.0)],
        [("HW-7160", "Winter service: gritter calibration and salt barn inspection", "WIN-100", 1840.00, 0.2),
         ("HW-7161", "Pothole repairs, Ward 5 (22 no.)", "HWY-310", 11260.00, 0.2),
         ("HW-7162", "Traffic signal maintenance, Victoria Square junction", "SIG-200", 3925.00, 0.2)],
    ]
    break_after = (2, 2)   # page break after the 3rd order of week 3: the week continues on page 2 without its date
    rows, part1, part2 = [], [], []
    style1, style2 = [], []
    run = [0.0, 0.0, 0.0]
    header = ["Order no.", "Description", "Cost centre", "Net (£)", "VAT (£)", "Gross (£)"]
    widths = [w * mm for w in COLUMN_WIDTHS_MM["TB-004"]]

    def gh(text):
        return [cell(text, CELLB), "", "", "", "", ""]

    target, tstyle = part1, style1
    for wi, (week, items) in enumerate(zip(weeks, orders)):
        target.append(gh(f"Week ending {long_date(week, weekday=True)}"))
        tstyle.append(("SPAN", (0, len(target)), (-1, len(target))))
        wsum = [0.0, 0.0, 0.0]
        for oi, (no, desc, cc, net, rate) in enumerate(items):
            vat = round(net * rate, 2)
            gross = round(net + vat, 2)
            rows.append({"week_ending": week, "order_no": no, "description": desc, "cost_centre": cc, "net_gbp": net, "vat_gbp": vat, "gross_gbp": gross})
            target.append([no, desc, cc, money(net), money(vat), money(gross)])
            for k, v in enumerate((net, vat, gross)):
                wsum[k] = round(wsum[k] + v, 2)
                run[k] = round(run[k] + v, 2)
            if (wi, oi) == break_after:
                target.append(bold_row(["", "Carried forward", "", money(run[0]), money(run[1]), money(run[2])], (3, 4, 5)))
                tstyle.append(("LINEABOVE", (3, len(target)), (-1, len(target)), 0.5, colors.black))
                target, tstyle = part2, style2
                target.append(bold_row(["", "Brought forward", "", money(run[0]), money(run[1]), money(run[2])], (3, 4, 5)))
        target.append(bold_row(["", "Week total", "", money(wsum[0]), money(wsum[1]), money(wsum[2])], (3, 4, 5)))
        tstyle.append(("LINEABOVE", (3, len(target)), (-1, len(target)), 0.4, colors.HexColor("#555555")))
    target.append(bold_row(["", "Total for July 2026", "", money(run[0]), money(run[1]), money(run[2])], (3, 4, 5)))
    tstyle.append(("LINEABOVE", (0, len(target)), (-1, len(target)), 0.8, colors.black))

    t1 = table(header, part1, widths, right=(3, 4, 5), header_rule=True, extra_style=style1)
    t2 = table(header, part2, widths, right=(3, 4, 5), header_rule=True, extra_style=style2)
    intro = [P(council["name"], "title"),
             P("Highways term maintenance contract: payments statement, quarter 2 (July 2026)", "subtitle"),
             P(f"Contractor: {supplier['name']}. Contract reference {council['short']}/HWY/2023/07. Orders certified for payment, "
               "grouped by the week in which they were certified. VAT is charged at 20 per cent except on permit fees, which are "
               "outside the scope of VAT.", "body"),
             Spacer(1, 2 * mm)]
    story = intro + [t1, PageBreak(), t2, Spacer(1, 4 * mm),
                     P("Certified by the Highways Contract Manager. Payment within 30 days of a valid invoice.", "small")]
    build_pdf(path, story, council["name"], "Highways payments statement Q2", header=f"{council['name']} · {council['short']}/HWY/2023/07 · payments statement")
    if page_count(path) != 2:
        raise RuntimeError(f"TB-004: expected 2 pages, got {page_count(path)}")
    p1, p2 = pdftext(path, 1), pdftext(path, 2)
    if "Carried forward" not in p1 or "HW-7143" in p1:
        raise RuntimeError("TB-004: the page break is not where the case needs it")
    cut = weeks[break_after[0]]
    if uk(cut) in p2 or long_date(cut) in p2:
        raise RuntimeError("TB-004: the week cut by the page break is named on page 2")
    return {
        "case_id": "TB-004", "slug": "weekly-statement-across-page",
        "title": "Borderless weekly payments statement: date headings, subtotals and a week split across the page",
        "columns": [("week_ending", "Week ending (group heading)", "date"), ("order_no", "Order no.", "id"), ("description", "Description", "text"),
                    ("cost_centre", "Cost centre", "id"), ("net_gbp", "Net (£)", "money"), ("vat_gbp", "VAT (£)", "money"), ("gross_gbp", "Gross (£)", "money")],
        "rows": rows,
        "hazards": ["no_ruling_lines", "group_header_rows", "subtotal_rows", "table_crosses_page", "group_crosses_page",
                    "carried_forward_rows", "repeated_header", "footer_interleaves", "wrapped_cells", "totals_row"],
        "row_rules": "One row per order. The week-ending date comes from the heading above the order's group; the orders at the top of "
                     "page 2 still belong to the week that started on page 1. Week headings, week totals, Carried forward, Brought "
                     "forward and the Total for July 2026 are not rows.",
        "why": "The date is not in any row: it is a heading above each week's orders, and the week of 17 July is cut by the page "
               "break, so the two orders at the top of page 2 sit under no heading of their own: their week is named only on "
               "page 1 (the week totals carry no date). Subtotal, carried-forward and brought-forward lines look like data rows, "
               "there are no ruled lines, and long descriptions wrap.",
        "naive_stop": r"^\s*Total for July 2026\b",
    }


CASES = [tb001, tb002, tb003, tb004]


def write_case(fn, out_dir: str, dpi: int = 110) -> dict:
    tmp_pdf = os.path.join(out_dir, "_tmp.pdf")
    spec = fn(tmp_pdf)
    cid = spec["case_id"]
    case_dir = os.path.join(out_dir, cid)
    if os.path.isdir(case_dir):
        shutil.rmtree(case_dir)
    os.makedirs(os.path.join(case_dir, "pages"))
    os.replace(tmp_pdf, os.path.join(case_dir, "document.pdf"))
    cols = spec["columns"]
    with open(os.path.join(case_dir, "expected.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow([c[0] for c in cols])
        for r in spec["rows"]:
            out = []
            for name, _hdr, typ in cols:
                v = r[name]
                out.append(v.isoformat() if typ == "date" else num(v) if typ == "money" else ("" if v is None else str(v)))
            w.writerow(out)
    subprocess.run(["pdftoppm", "-r", str(dpi), "-png", os.path.join(case_dir, "document.pdf"), os.path.join(case_dir, "pages", "page")], check=True)
    pngs = sorted(os.listdir(os.path.join(case_dir, "pages")))
    for i, n in enumerate(pngs, 1):
        os.rename(os.path.join(case_dir, "pages", n), os.path.join(case_dir, "pages", f"page-{i}.png"))
    with open(os.path.join(case_dir, "text.txt"), "w", encoding="utf-8") as fh:
        fh.write(pdftext(os.path.join(case_dir, "document.pdf")))
    pages = page_count(os.path.join(case_dir, "document.pdf"))
    meta = {"case_id": cid, "slug": spec["slug"], "title": spec["title"], "pages": pages, "rows": len(spec["rows"]),
            "columns": [{"name": n, "printed_header": h, "type": t} for n, h, t in cols], "hazards": spec["hazards"],
            "row_rules": spec["row_rules"], "why": spec["why"], "naive_stop": spec["naive_stop"],
            "document": f"{cid}/document.pdf", "expected": f"{cid}/expected.csv", "layout": spec.get("layout", {})}
    with open(os.path.join(case_dir, "spec.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2, ensure_ascii=False)
    with open(os.path.join(case_dir, "notes.md"), "w", encoding="utf-8") as fh:
        fh.write(f"# {cid} - {spec['title']}\n\n{spec['why']}\n\n## What counts as a row\n\n{spec['row_rules']}\n\n## Hazards\n\n")
        fh.write("".join(f"- **{h}**: {HAZARDS[h]}\n" for h in spec["hazards"]))
        fh.write("\n## Columns\n\n| CSV column | Printed header | Type |\n|---|---|---|\n")
        fh.write("".join(f"| `{n}` | {h} | {t} |\n" for n, h, t in cols))
    return meta


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="tables")
    ap.add_argument("--only")
    args = ap.parse_args(argv)
    require_tools("pdftoppm", "pdftotext")
    os.makedirs(args.out, exist_ok=True)
    metas = []
    for fn in CASES:
        if args.only and fn.__name__.upper().replace("TB", "TB-") not in args.only.split(","):
            continue
        m = write_case(fn, args.out)
        metas.append(m)
        print(m["case_id"], m["pages"], "pages,", m["rows"], "rows:", m["title"])
    if not args.only:
        manifest = {"track": "tables", "generator_version": GENERATOR_VERSION,
                    "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "hazards": {h: HAZARDS[h] for h in HAZARDS}, "cases": metas}
        with open(os.path.join(args.out, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    main()
