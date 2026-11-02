"""Synthetic case generator.

Each case is a spec (gold fields + rendering style + hazards). The same spec
drives both the PDF and gold.json, so labels are exact by construction.

Run:  python -m crtest.gen --out cases
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field

from reportlab.lib.units import mm
from reportlab.platypus import FrameBreak, KeepTogether, NextPageTemplate, PageBreak, Spacer

from . import corpus, pa2023
from .sysdeps import require_tools
from .render import (
    MARGIN, PAGE_W, P, build_pdf, data_table, fmt_cpv, fmt_date, fmt_money, kv_table_borderless_grouped,
    kv_table_grid, months_between, pdf_page_count, rasterise_to_scanned_pdf, signature_block, stacked_kv,
)

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_PATH = os.path.join(HERE, "..", "schema", "fields.json")
GENERATOR_VERSION = "0.1.0"

with open(SCHEMA_PATH, encoding="utf-8") as _f:
    SCHEMA = json.load(_f)
FIELD_NAMES = [f["name"] for f in SCHEMA["fields"]]


def blank_fields() -> dict:
    return {k: None for k in FIELD_NAMES}


def add_months(d: dt.date, n: int) -> dt.date:
    y = d.year + (d.month - 1 + n) // 12
    m = (d.month - 1 + n) % 12 + 1
    day = min(d.day, [31, 29 if y % 4 == 0 and (y % 100 != 0 or y % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1])
    return dt.date(y, m, day)


def term_end(start: dt.date, months: int) -> dt.date:
    return add_months(start, months) - dt.timedelta(days=1)


def D(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


@dataclass
class CaseSpec:
    case_id: str
    slug: str
    title: str
    hazards: list[str]
    notes: str
    fields: dict
    style: dict = field(default_factory=dict)
    council: dict = field(default_factory=dict)
    supplier: dict = field(default_factory=dict)
    service: dict = field(default_factory=dict)
    scanned: bool = False
    extra: dict = field(default_factory=dict)


HAZARD_DESCRIPTIONS = {
    "baseline": "Plain single-column notice with bordered key/value table. Should be easy.",
    "table_crosses_page": "A table runs over a page boundary; the header row repeats on the next page.",
    "repeated_header": "Repeated table header rows must not be read as data rows.",
    "footer_interleaves": "Page footer text (authority, page x of y) falls between table rows in a text dump.",
    "annual_vs_total_value": "An annual price and a total contract value both appear; only the total is the contract value.",
    "borderless_table": "Key/value table has no rules; column boundaries must be inferred from alignment.",
    "grouped_rows": "A section label is printed once and spans several rows (blank cells below it).",
    "label_synonyms": "Fields use non-standard labels (e.g. 'Initial expiry' for the end date).",
    "two_column_layout": "Page is laid out in two columns; a line-based text dump interleaves them.",
    "stacked_label_value": "Each label is printed on one line and its value on the next.",
    "money_short_format": "Money printed as £2.31m rather than £2,310,000.",
    "money_spaced_format": "Money printed with spaces as thousands separators (£4 200 000).",
    "money_words_and_figures": "Money printed in words followed by figures.",
    "money_gbp_suffix": "Money printed as 1,234.00 GBP.",
    "date_uk_format": "Dates printed as DD/MM/YYYY.",
    "date_iso_format": "Dates printed as YYYY-MM-DD.",
    "date_ordinal": "Dates printed as 3rd March 2026.",
    "date_abbr": "Dates printed as 03-Mar-2026.",
    "scanned_no_text_layer": "Image-only PDF (simulated photocopy): no text layer, slight skew and noise. Needs OCR or vision.",
    "trading_as_name": "Supplier printed with a trading name in brackets; the legal name is the answer.",
    "supplier_name_wrapped": "Supplier legal name wraps across two lines mid-name.",
    "multi_lot": "Several lots awarded to different suppliers.",
    "unawarded_lot": "One lot was not awarded; it must appear with awarded=false and no supplier.",
    "totals_row": "A totals row must not be read as a lot or a line item.",
    "percent_not_printed": "The modification percentage is not printed and must be computed from the values.",
    "values_in_prose": "Key values are embedded in running text rather than in a table.",
    "negative_control": "Nothing is wrong with this document; a rule engine should raise no high-severity flag.",
    "wrong_legal_ground": "The authority cites a modification ground that does not cover the change made.",
    "retrospective_extension": "The variation is dated after the contract had already expired.",
    "no_ccn_published": "The document states that no contract change notice was published before the modification.",
    "standstill_too_short": "The stated standstill end date is fewer than 8 working days after publication.",
    "direct_award": "Direct award under section 41 with a stated justification.",
    "kpi_shortfall": "Contract over £5m with fewer than three KPIs.",
    "late_contract_details_notice": "Contract details notice published more than 30 days after signature.",
    "payment_terms_over_30": "Stated payment terms exceed 30 days.",
    "evaluation_scores_table": "Tenderer scoring table that could be mistaken for lot values.",
}


# ====================================================================== case specs
def _common(council_idx: int, supplier_idx: int, service_idx: int, notice_type: str) -> tuple[dict, dict, dict, dict]:
    c = corpus.COUNCILS[council_idx]
    sv = corpus.SERVICES[service_idx]
    s = corpus.SUPPLIERS[supplier_idx]
    f = blank_fields()
    f.update({
        "notice_type": notice_type,
        "contracting_authority": c["name"],
        "contracting_authority_type": c["type"],
        "supplier_name": s["name"],
        "supplier_registration_number": s["reg"],
        "contract_title": sv["title"],
        "procurement_type": sv["type"],
        "light_touch": False,
        "cpv_codes": list(sv["cpv"]),
        "lots": [],
        "value_includes_vat": False,
        "extension_options_months": 0,
    })
    return f, c, s, sv


def _set_term(f: dict, start: str, months: int, options: int = 0) -> None:
    s = D(start)
    f["contract_start_date"] = s.isoformat()
    f["contract_end_date"] = term_end(s, months).isoformat()
    f["extension_options_months"] = options
    f["maximum_term_months"] = months + options


def _set_modification(f: dict, n: int, date: str, desc: str, ground: str, orig_value: float, delta_value: float,
                      orig_end: str, term_delta_months: int, ccn_published: bool, voluntary_standstill: bool) -> None:
    if f["notice_type"] == "contract_change_notice":
        f["contract_end_date"] = None  # a change notice prints the pre- and post-modification end dates instead
    f["modification_number"] = n
    f["modification_date"] = date
    f["modification_description"] = desc
    f["modification_ground"] = ground
    f["original_contract_value_gbp"] = orig_value
    f["revised_contract_value_gbp"] = orig_value + delta_value
    f["value_change_gbp"] = delta_value
    f["value_change_percent"] = round(100.0 * delta_value / orig_value, 1)
    f["original_end_date"] = orig_end
    f["revised_end_date"] = add_months(D(orig_end) + dt.timedelta(days=1), term_delta_months).__sub__(dt.timedelta(days=1)).isoformat() if term_delta_months else orig_end
    f["term_change_months"] = term_delta_months
    f["contract_change_notice_published"] = ccn_published
    f["voluntary_standstill"] = voluntary_standstill


def build_specs() -> list[CaseSpec]:
    specs: list[CaseSpec] = []

    # CR-001 ------------------------------------------------------------ baseline award notice
    f, c, s, sv = _common(0, 5, 0, "contract_award_notice")
    f.update({"notice_reference": "003118-2026", "contract_reference": "WBC/2026/0071", "procedure": "open",
              "notice_published_date": "2026-02-16", "award_date": "2026-02-12", "standstill_end_date": "2026-02-26",
              "number_of_tenders_received": 4, "contract_value_gbp": 1_240_000, "payment_terms_days": 30})
    _set_term(f, "2026-04-01", 48, 24)
    specs.append(CaseSpec("CR-001", "award-notice-clean", "Contract award notice, clean layout", ["baseline"],
                          "Baseline. Bordered key/value table, long-form dates, standard money. Every extractor should get this right.",
                          f, {"date": "long", "money": "standard", "kv": "grid"}, c, s, sv))

    # CR-002 ------------------------------------------------------------ CDN with table crossing a page
    f, c, s, sv = _common(1, 8, 4, "contract_details_notice")
    f.update({"notice_reference": "004213-2026", "contract_reference": "HDC-ENV-2025-014", "procedure": "competitive_flexible",
              "notice_published_date": "2026-03-20", "award_date": "2026-02-24", "contract_signed_date": "2026-03-03",
              "number_of_tenders_received": 3, "contract_value_gbp": 27_650_000, "payment_terms_days": 30,
              "kpis": ["Missed collections per 100,000", "Recycling rate (%)", "Customer satisfaction score (quarterly survey)"]})
    _set_term(f, "2026-04-06", 84, 24)
    elements = [
        ("1.1", "Residual waste collection (fortnightly)", "1,650,000.00"), ("1.2", "Dry recycling collection (fortnightly)", "740,000.00"),
        ("1.3", "Food waste collection (weekly)", "395,000.00"), ("1.4", "Garden waste subscription service", "212,500.00"),
        ("1.5", "Bulky waste collection", "61,200.00"), ("1.6", "Clinical waste collection", "18,900.00"),
        ("1.7", "Assisted collections", "44,300.00"), ("1.8", "Communal bin servicing (flats)", "96,750.00"),
        ("1.9", "Bring-site servicing", "27,400.00"), ("1.10", "Container delivery and replacement", "38,600.00"),
        ("2.1", "Depot and fleet (incl. 14 RCVs)", "402,000.00"), ("2.2", "Fleet maintenance and MOT", "88,000.00"),
        ("2.3", "Fuel (indexed, baseline)", "131,000.00"), ("2.4", "Telematics and in-cab devices", "9,850.00"),
        ("3.1", "Contract management and reporting", "52,000.00"), ("3.2", "Customer contact handling", "21,000.00"),
        ("3.3", "Education and engagement programme", "12,000.00"), ("3.4", "Social value commitments", "0.00"),
        ("4.1", "Christmas and bank-holiday catch-up", "16,500.00"), ("4.2", "Severe weather contingency", "11,000.00"),
        ("4.3", "Missed collection rectification", "0.00"), ("4.4", "Street-side bin cleansing (optional)", "24,000.00"),
    ]
    specs.append(CaseSpec("CR-002", "cdn-table-crosses-page", "Contract details notice with a priced schedule crossing a page",
                          ["table_crosses_page", "repeated_header", "footer_interleaves", "annual_vs_total_value", "totals_row"],
                          "The priced schedule of service elements runs over the page break with a repeated header and the footer lands between rows. "
                          "The value row prints the ANNUAL price first (£3,950,000 per annum) and the contract value second (£27,650,000 over 84 months); "
                          "the schedule total is also the annual figure. Only £27,650,000 is correct.",
                          f, {"date": "long", "money": "standard", "kv": "grid", "footer": "standard"}, c, s, sv, extra={"elements": elements, "annual_total": "3,950,000.00",
                                 "value_text": "£3,950,000.00 per annum; £27,650,000.00 over the 84-month initial term"}))

    # CR-003 ------------------------------------------------------------ borderless grouped particulars (works)
    f, c, s, sv = _common(2, 2, 1, "contract_details_notice")
    f.update({"notice_reference": "002907-2026", "contract_reference": "NVCC/HW/0442", "procedure": "open",
              "notice_published_date": "2026-02-05", "award_date": "2026-01-09", "contract_signed_date": "2026-01-20",
              "number_of_tenders_received": 5, "contract_value_gbp": 6_400_000, "payment_terms_days": 30,
              "kpis": ["Emergency call-outs attended within 2 hours", "Pothole repairs completed within 5 working days", "Winter gritting routes completed to schedule"]})
    _set_term(f, "2026-03-01", 36, 12)
    specs.append(CaseSpec("CR-003", "cdn-borderless-grouped-particulars", "Contract details notice with borderless, grouped particulars",
                          ["borderless_table", "grouped_rows", "label_synonyms", "annual_vs_total_value", "date_abbr"],
                          "Contract particulars are a borderless three-column table whose first column is a section label spanning several rows. "
                          "Labels are non-standard ('Initial expiry', 'Estimated total (ex VAT)'), an annual figure sits next to the total, and dates are 01-Mar-2026 style.",
                          f, {"date": "abbr", "money": "standard", "kv": "borderless_grouped"}, c, s, sv))

    # CR-004 ------------------------------------------------------------ CCN citing the wrong ground
    f, c, s, sv = _common(3, 0, 6, "contract_change_notice")
    f.update({"notice_reference": "006540-2026", "contract_reference": "SMBC/FM/2025/003", "procedure": "competitive_flexible",
              "notice_published_date": "2026-05-04", "award_date": "2025-11-14", "contract_signed_date": "2026-03-20"})
    _set_term(f, "2026-04-01", 36, 0)
    _set_modification(f, 1, "2026-05-11", "Addition of three annexe buildings to the cleaning and M&E maintenance scope following the transfer of the Riverside offices to the Council, and a 12-month extension of the term to align with the corporate accommodation strategy.",
                      "s74_below_threshold", 1_800_000, 260_000, f["contract_end_date"], 12, True, True)
    specs.append(CaseSpec("CR-004", "ccn-wrong-ground-value-and-term", "Contract change notice relying on the wrong ground",
                          ["wrong_legal_ground", "percent_not_printed", "date_uk_format"],
                          "The notice cites section 74(1)(c) (below-threshold) for a +14.4% value change and a +33% term extension. "
                          "The percentage is not printed. A rule engine should raise PA23-s74-exceeds-below-threshold and PA23-s74-substantial-term.",
                          f, {"date": "uk", "money": "standard", "kv": "grid", "print_percent": False}, c, s, sv))

    # CR-005 ------------------------------------------------------------ scanned contract particulars, KPI shortfall
    f, c, s, sv = _common(4, 7, 5, "contract_document")
    f.update({"contract_reference": "ECDC/LEI/2026/01", "procedure": "competitive_flexible", "award_date": "2026-03-27",
              "contract_signed_date": "2026-04-14", "number_of_tenders_received": 2, "contract_value_gbp": 9_750_000,
              "payment_terms_days": 30, "kpis": ["Facility availability (opening hours delivered)", "Quarterly customer satisfaction score"]})
    _set_term(f, "2026-06-01", 60, 0)
    specs.append(CaseSpec("CR-005", "contract-scanned-particulars", "Scanned contract particulars (image-only PDF)",
                          ["scanned_no_text_layer", "trading_as_name", "kpi_shortfall"],
                          "The contract front matter is an image-only PDF with slight skew and speckle, as if photocopied. pdftotext returns nothing. "
                          "Supplier is printed as 'Halbrook Leisure Operations Ltd (trading as Halbrook Active)'. Only two KPIs are set on a £9.75m contract.",
                          f, {"date": "long", "money": "standard", "kv": "grid"}, c, s, sv, scanned=True))

    # CR-006 ------------------------------------------------------------ two-column stacked, late CDN, 45-day terms
    f, c, s, sv = _common(5, 4, 2, "contract_details_notice")
    f.update({"notice_reference": "008862-2026", "contract_reference": "KCC-ICT-24-0917", "procedure": "framework_call_off",
              "notice_published_date": "2026-04-29", "award_date": "2026-02-20", "contract_signed_date": "2026-02-27",
              "number_of_tenders_received": 3, "contract_value_gbp": 2_310_000, "payment_terms_days": 45,
              "kpis": ["First-time fix rate", "Service availability (monthly)", "Customer satisfaction score (quarterly survey)"]})
    _set_term(f, "2026-04-01", 36, 24)
    specs.append(CaseSpec("CR-006", "cdn-two-column-stacked", "Contract details notice in a two-column stacked layout",
                          ["two_column_layout", "stacked_label_value", "money_short_format", "trading_as_name", "late_contract_details_notice", "payment_terms_over_30"],
                          "Labels and values are stacked in a narrow left column beside a narrative right column, so a line-based text dump interleaves them. "
                          "Value is printed as £2.31m. Published 61 days after signature; payment terms 45 days.",
                          f, {"date": "long", "money": "short", "kv": "stacked_two_column"}, c, s, sv))

    # CR-007 ------------------------------------------------------------ multi-lot award with unawarded lot and short standstill
    f, c, s, sv = _common(2, 9, 3, "contract_award_notice")
    f.update({"notice_reference": "005077-2026", "contract_reference": "NVCC/ASC/2025/0210", "procedure": "competitive_flexible",
              "light_touch": True, "notice_published_date": "2026-03-09", "award_date": "2026-03-06", "standstill_end_date": "2026-03-17",
              "number_of_tenders_received": 7, "contract_value_gbp": 3_430_000, "payment_terms_days": 30,
              "lots": [
                  {"lot_number": 1, "lot_title": "Domiciliary care - North locality", "supplier_name": corpus.SUPPLIERS[9]["name"], "value_gbp": 1_980_000, "awarded": True},
                  {"lot_number": 2, "lot_title": "Domiciliary care - South locality", "supplier_name": corpus.SUPPLIERS[3]["name"], "value_gbp": 1_450_000, "awarded": True},
                  {"lot_number": 3, "lot_title": "Reablement and rapid response", "supplier_name": None, "value_gbp": None, "awarded": False},
              ]})
    _set_term(f, "2026-05-01", 36, 24)
    specs.append(CaseSpec("CR-007", "award-multi-lot-unawarded-lot", "Multi-lot award notice with an unawarded lot and a short standstill",
                          ["multi_lot", "unawarded_lot", "totals_row", "evaluation_scores_table", "table_crosses_page", "standstill_too_short"],
                          "Three lots, two awarded to different suppliers, lot 3 not awarded. The evaluation scores table crosses the page. "
                          "Standstill ends 6 working days after publication (8 required). Light touch contract.",
                          f, {"date": "long", "money": "nodp", "kv": "grid"}, c, s, sv))

    # CR-008 ------------------------------------------------------------ CCN below threshold, negative control
    f, c, s, sv = _common(0, 1, 8, "contract_change_notice")
    f.update({"notice_reference": "007311-2026", "contract_reference": "WBC/2025/0119", "procedure": "open",
              "notice_published_date": "2026-06-02", "award_date": "2025-09-30", "contract_signed_date": "2025-10-28"})
    _set_term(f, "2025-12-01", 60, 24)
    _set_modification(f, 2, "2026-06-15", "Uplift to the schedule of rates to reflect the 2026/27 National Living Wage increase, applied to the cleansing operatives' labour element only.",
                      "s74_below_threshold", 4_200_000, 252_000, f["contract_end_date"], 0, True, False)
    specs.append(CaseSpec("CR-008", "ccn-below-threshold-negative-control", "Contract change notice correctly below threshold (negative control)",
                          ["negative_control", "money_spaced_format", "date_uk_format", "percent_not_printed"],
                          "A +6.0% uplift with no term change, correctly made under section 74(1)(c). Money is printed as £4 200 000. "
                          "No high-severity flag should be raised; this catches over-eager rule engines.",
                          f, {"date": "uk", "money": "spaced", "kv": "grid", "print_percent": False}, c, s, sv))

    # CR-009 ------------------------------------------------------------ deed of variation after expiry, no CCN
    f, c, s, sv = _common(1, 5, 0, "contract_document")
    f.update({"contract_reference": "HDC-PKS-2021-07", "procedure": "open", "award_date": "2021-02-11", "contract_signed_date": "2021-03-12"})
    _set_term(f, "2021-04-01", 60, 0)
    _set_modification(f, 1, "2026-05-15", "Extension of the contract term by 12 months and an increase in the annual contract sum to cover the additional 14 hectares of open space transferred from the Parish Council.",
                      "s74_non_substantial", 1_550_000, 310_000, f["contract_end_date"], 12, False, False)
    specs.append(CaseSpec("CR-009", "deed-of-variation-retrospective", "Deed of variation extending an expired contract with no change notice",
                          ["retrospective_extension", "no_ccn_published", "wrong_legal_ground", "values_in_prose", "date_ordinal"],
                          "A deed of variation dated 15 May 2026 extends a contract that expired on 31 March 2026, adds 20% to the value, and the decision record "
                          "states no contract change notice was published. Key values are in prose recitals. Expect three high flags.",
                          f, {"date": "ordinal", "money": "standard", "kv": "prose"}, c, s, sv))

    # CR-010 ------------------------------------------------------------ works CDN, wrapped supplier name, words and figures
    f, c, s, sv = _common(3, 6, 7, "contract_details_notice")
    f.update({"notice_reference": "009104-2026", "contract_reference": "SMBC/CAP/2025/EC-MILL", "procedure": "open",
              "notice_published_date": "2026-05-22", "award_date": "2026-04-17", "contract_signed_date": "2026-05-05",
              "number_of_tenders_received": 6, "contract_value_gbp": 7_980_000, "payment_terms_days": 30,
              "kpis": ["Programme milestones achieved on time", "Defects at practical completion", "Accident frequency rate", "Local labour and apprenticeships delivered"]})
    _set_term(f, "2026-06-15", 24, 0)
    specs.append(CaseSpec("CR-010", "cdn-works-wrapped-name-words-figures", "Works contract details notice with wrapped supplier name and value in words",
                          ["supplier_name_wrapped", "money_words_and_figures", "date_ordinal"],
                          "The supplier's legal name wraps across two lines inside a narrow cell, and the contract sum is printed in words then figures. Four KPIs, 30-day terms: compliant.",
                          f, {"date": "ordinal", "money": "words", "kv": "grid", "narrow_supplier": True}, c, s, sv))

    # CR-011 ------------------------------------------------------------ works CCN over the 50% cap, s.77 trigger
    f, c, s, sv = _common(5, 2, 1, "contract_change_notice")
    f.update({"notice_reference": "009877-2026", "contract_reference": "KCC-HWY-2025-0033", "procedure": "open",
              "notice_published_date": "2026-06-18", "award_date": "2025-12-05", "contract_signed_date": "2026-01-16"})
    _set_term(f, "2026-02-01", 36, 12)
    _set_modification(f, 1, "2026-06-29", "Additional carriageway reconstruction on the A-road network following the February 2026 flood damage, delivered under the existing schedule of rates.",
                      "sch8_para3_unforeseeable", 6_100_000, 3_660_000, f["contract_end_date"], 0, True, True)
    specs.append(CaseSpec("CR-011", "ccn-works-over-50pc-cap", "Works change notice exceeding the 50% unforeseeable-circumstances cap",
                          ["percent_not_printed", "money_gbp_suffix", "date_iso_format"],
                          "A +60% increase under Schedule 8 paragraph 3, which caps increases at 50%. The contract is over £5m, so section 77 publication of the modified contract applies. Money as 6,100,000.00 GBP; dates ISO.",
                          f, {"date": "iso", "money": "gbp_suffix", "kv": "grid", "print_percent": False}, c, s, sv))

    # CR-012 ------------------------------------------------------------ direct award in prose
    corpus_service = {"title": "Emergency Flood Response and Debris Clearance", "type": "services", "cpv": ["90470000", "90700000"], "supplier": 8}
    f, c, s, sv = _common(4, 8, 4, "contract_award_notice")
    sv = corpus_service
    f.update({"contract_title": sv["title"], "cpv_codes": list(sv["cpv"]), "notice_reference": "001206-2026", "contract_reference": "ECDC/EMG/2026/02",
              "procedure": "direct_award", "notice_published_date": "2026-01-22", "award_date": "2026-01-19", "standstill_end_date": None,
              "number_of_tenders_received": 1, "contract_value_gbp": 420_000, "payment_terms_days": 30})
    _set_term(f, "2026-01-20", 6, 0)
    specs.append(CaseSpec("CR-012", "award-direct-award-prose", "Direct award notice with values embedded in prose",
                          ["direct_award", "values_in_prose", "date_iso_format"],
                          "Section 41 direct award (urgency). There is no key/value table: authority, supplier, value, dates and CPV codes are all inside paragraphs. No standstill applies.",
                          f, {"date": "iso", "money": "nodp", "kv": "prose"}, c, s, sv))
    return specs


# ====================================================================== document builders
def _kv_rows_award(spec: CaseSpec) -> dict:
    f, st, c, s = spec.fields, spec.style, spec.council, spec.supplier
    ds, ms = st["date"], st["money"]
    sup = s["name"] + (f" (trading as {s['ta']})" if s.get("ta") and "trading_as_name" in spec.hazards else "")
    return {
        "authority": [
            ("Contracting authority", f["contracting_authority"]),
            ("Address", c["address"]),
            ("Contact", c["contact"]),
            ("Authority type", "Sub-central contracting authority" if f["contracting_authority_type"] == "sub_central" else "Central government authority"),
        ],
        "procurement": [
            ("Title", f["contract_title"]),
            ("Contract reference", f["contract_reference"]),
            ("Nature of the contract", f["procurement_type"].capitalize() + (" (light touch contract)" if f.get("light_touch") else "")),
            ("CPV codes", fmt_cpv(f["cpv_codes"])),
            ("Procedure", corpus.PROCEDURE_LABEL[f["procedure"]]),
            ("Tenders received", str(f["number_of_tenders_received"])),
        ],
        "award": [
            ("Date of award decision", fmt_date(f["award_date"], ds)),
            ("Supplier", sup),
            ("Supplier registration number", f["supplier_registration_number"]),
            ("Supplier address", s["address"]),
            ("Contract value (excluding VAT)", fmt_money(f["contract_value_gbp"], ms)),
            ("Contract start date", fmt_date(f["contract_start_date"], ds)),
            ("Contract end date", fmt_date(f["contract_end_date"], ds)),
            ("Extension options", f"{f['extension_options_months']} months" if f["extension_options_months"] else "None"),
            ("Maximum term (including options)", f"{f['maximum_term_months']} months"),
            ("Payment terms", f"{f['payment_terms_days']} days from receipt of a valid invoice"),
        ],
    }


def story_award_notice(spec: CaseSpec) -> list:
    f, st = spec.fields, spec.style
    ds, ms = st["date"], st["money"]
    story = [P("Contract award notice", "title"),
             P(f"Procurement Act 2023, section 50 &nbsp;·&nbsp; Notice reference {f['notice_reference']} &nbsp;·&nbsp; Published {fmt_date(f['notice_published_date'], ds)}", "subtitle")]
    if st["kv"] == "prose":
        return story + _story_award_prose(spec)
    rows = _kv_rows_award(spec)
    story += [P("1. Contracting authority", "h2"), kv_table_grid(rows["authority"]),
              P("2. Procurement", "h2"), kv_table_grid(rows["procurement"])]
    if f["lots"]:
        story += [P("3. Award by lot", "h2")]
        lot_rows = []
        for lot in f["lots"]:
            if lot["awarded"]:
                lot_rows.append([str(lot["lot_number"]), lot["lot_title"], lot["supplier_name"], fmt_money(lot["value_gbp"], ms)])
            else:
                lot_rows.append([str(lot["lot_number"]), lot["lot_title"], "Not awarded - no compliant tenders received", "-"])
        width = PAGE_W - 2 * MARGIN
        story.append(data_table(["Lot", "Description", "Awarded to", "Value (ex VAT)"], lot_rows,
                                [12 * mm, width * 0.36, width * 0.38, width - 12 * mm - width * 0.74],
                                total_row=["", "Total value awarded", "", fmt_money(f["contract_value_gbp"], ms)], align_right_cols=(3,)))
        story += [Spacer(1, 4), P("Contract dates apply to all awarded lots.", "small")]
        award_rows = [r for r in rows["award"] if r[0] not in ("Supplier", "Supplier registration number", "Supplier address", "Contract value (excluding VAT)")]
        story += [P("4. Contract", "h2"), kv_table_grid(award_rows)]
        story += [P("5. Assessment summary", "h2"),
                  P("Scores awarded to each tenderer under the published award criteria (quality 60%, price 40%). Scores are out of 100.")]
        tenderers = ["Juniper Social Care Services Ltd", "Dunmore Care Partnership CIC", "Meadowcroft Homecare Ltd", "Oakwood Community Support CIC",
                     "Sunrise Domiciliary Care Ltd", "Harbourside Care Group Ltd", "Vale Independent Living Ltd"]
        score_rows = []
        import random
        rnd = random.Random(7)
        for i, t in enumerate(tenderers):
            for lot in (1, 2, 3):
                if rnd.random() < 0.35 and i > 1:
                    continue
                q = rnd.randint(48, 92)
                pr = rnd.randint(55, 100)
                score_rows.append([t, f"Lot {lot}", str(q), str(pr), f"{0.6 * q + 0.4 * pr:.1f}", "Awarded" if (i == 0 and lot == 1) or (i == 1 and lot == 2) else "Unsuccessful"])
        story.append(data_table(["Tenderer", "Lot", "Quality (60)", "Price (40)", "Total", "Outcome"], score_rows,
                                [width * 0.36, 16 * mm, 22 * mm, 20 * mm, 18 * mm, width - width * 0.36 - 76 * mm], align_right_cols=(2, 3, 4)))
        story += [P("6. Standstill", "h2"),
                  P(f"The standstill period ends on {fmt_date(f['standstill_end_date'], ds)}. The authority will not enter into the contracts before that date. "
                    "Assessment summaries have been provided to all tenderers.")]
    else:
        story += [P("3. Award", "h2"), kv_table_grid(rows["award"]),
                  P("4. Standstill period", "h2")]
        if f["standstill_end_date"]:
            story.append(P(f"The mandatory standstill period of eight working days ends on {fmt_date(f['standstill_end_date'], ds)}. "
                           "The authority will not enter into the contract before the end of the standstill period."))
        else:
            story.append(P("Not applicable."))
    story += [Spacer(1, 6), P("This notice has been published on the central digital platform in accordance with section 50 of the Procurement Act 2023. "
                              "Assessment summaries were provided to each tenderer on the date of this notice.", "small")]
    return story


def _story_award_prose(spec: CaseSpec) -> list:
    f, c, s, st = spec.fields, spec.council, spec.supplier, spec.style
    ds, ms = st["date"], st["money"]
    return [
        P("1. Summary", "h2"),
        P(f"{f['contracting_authority']} (the Council), of {c['address']}, gives notice that on {fmt_date(f['award_date'], ds)} it awarded a contract for "
          f"{f['contract_title']} (the Council's reference {f['contract_reference']}) to {s['name']}, company number {f['supplier_registration_number']}, "
          f"of {s['address']}."),
        P("2. Procedure and justification", "h2"),
        P(f"The contract was awarded by direct award under section 41 of the Procurement Act 2023 on the ground of extreme and unavoidable urgency following the "
          f"flooding of the Calder valley on 12 and 13 January 2026. The Council considered that the goods and services were required urgently to protect life and property and "
          f"that running a competitive tendering procedure was not possible in the time available. One supplier was approached and {f['number_of_tenders_received']} quotation was received. "
          "A transparency notice was published on 2026-01-16."),
        P("3. Contract", "h2"),
        P(f"The contract is a services contract. The relevant CPV codes are {f['cpv_codes'][0]} (Cleaning services) and {f['cpv_codes'][1]} (Environmental services). "
          f"The estimated value of the contract, excluding VAT, is {fmt_money(f['contract_value_gbp'], ms)}. The contract runs from {fmt_date(f['contract_start_date'], ds)} to "
          f"{fmt_date(f['contract_end_date'], ds)} with no option to extend, a maximum term of {f['maximum_term_months']} months. Payment terms are {f['payment_terms_days']} days from receipt of a valid invoice."),
        P("4. Standstill", "h2"),
        P("No standstill period applies to this award because the contract was awarded by direct award under section 41 on grounds of urgency. "
          f"This notice was published on {fmt_date(f['notice_published_date'], ds)}."),
        P("5. Contact", "h2"),
        P(f"Enquiries to {c['contact']}. Signed: {corpus.OFFICERS[4]}."),
    ]


def story_contract_details_notice(spec: CaseSpec) -> list:
    f, st, c, s = spec.fields, spec.style, spec.council, spec.supplier
    ds, ms = st["date"], st["money"]
    width = PAGE_W - 2 * MARGIN
    head = [P("Contract details notice", "title"),
            P(f"Procurement Act 2023, section 53 &nbsp;·&nbsp; Notice reference {f['notice_reference']} &nbsp;·&nbsp; Published {fmt_date(f['notice_published_date'], ds)}", "subtitle")]
    sup = s["name"] + (f" (trading as {s['ta']})" if s.get("ta") and "trading_as_name" in spec.hazards else "")

    if st["kv"] == "grid":
        ptype_label = f["procurement_type"].capitalize()
        value_label = "Contract sum (excluding VAT)" if f["procurement_type"] == "works" else "Total contract value (excluding VAT)"
        rows_a = [("Contracting authority", f["contracting_authority"]), ("Address", c["address"]), ("Contact", c["contact"])]
        rows_c = [
            ("Contract title", f["contract_title"]), ("Contract reference", f["contract_reference"]),
            ("Supplier", sup), ("Supplier registration number", f["supplier_registration_number"]),
            ("Nature of the contract", ptype_label), ("CPV codes", fmt_cpv(f["cpv_codes"])),
            ("Procedure", corpus.PROCEDURE_LABEL[f["procedure"]]), ("Tenders received", str(f["number_of_tenders_received"])),
            ("Date of award", fmt_date(f["award_date"], ds)), ("Date contract entered into", fmt_date(f["contract_signed_date"], ds)),
            ("Contract start date", fmt_date(f["contract_start_date"], ds)), ("Contract end date", fmt_date(f["contract_end_date"], ds)),
            ("Extension options", f"{f['extension_options_months']} months" if f["extension_options_months"] else "None"),
            ("Maximum term (including options)", f"{f['maximum_term_months']} months"),
            (value_label, spec.extra.get("value_text") or fmt_money(f["contract_value_gbp"], ms)),
            ("Payment terms", f"{f['payment_terms_days']} days from receipt of a valid invoice"),
        ]
        story = head + [P("1. Contracting authority", "h2"), kv_table_grid(rows_a), P("2. Contract", "h2")]
        if st.get("narrow_supplier"):
            story.append(kv_table_grid(rows_c, col_widths=(118 * mm, None)))
        else:
            story.append(kv_table_grid(rows_c))
        story += [P("3. Key performance indicators", "h2"),
                  P("The following key performance indicators have been set under section 52 of the Act:" if f["kpis"] else "None set.")]
        for i, k in enumerate(f["kpis"], 1):
            story.append(P(f"KPI {i}: {k}", "body"))
        if spec.extra.get("elements"):
            story += [P("4. Schedule of priced service elements (annual, year 1 prices, excluding VAT)", "h2"),
                      P("The annual price is payable in twelve equal monthly instalments and is subject to indexation from year 2 in accordance with Schedule 6 of the contract.")]
            rows = [[a, b, "£" + v] for a, b, v in spec.extra["elements"]]
            story.append(data_table(["Ref", "Service element", "Annual price"], rows, [16 * mm, width - 16 * mm - 40 * mm, 40 * mm],
                                    total_row=["", "Total annual price (year 1)", "£" + spec.extra["annual_total"]], align_right_cols=(2,)))
            story += [Spacer(1, 4), P(f"The total contract value of {fmt_money(f['contract_value_gbp'], ms)} is the estimated whole-life value over the initial term of 84 months, excluding extension options and VAT.", "small")]
        story += [Spacer(1, 6), P("A copy of the contract will be published in accordance with section 53(3) where the estimated value exceeds £5 million.", "small")]
        return story

    if st["kv"] == "borderless_grouped":
        groups = [
            ("Authority", [("Name", f["contracting_authority"]), ("Address", c["address"]), ("Contact", c["contact"])]),
            ("Contract", [("Title", f["contract_title"]), ("Reference", f["contract_reference"]), ("Nature", "Works"), ("CPV", fmt_cpv(f["cpv_codes"])),
                          ("Procedure", corpus.PROCEDURE_LABEL[f["procedure"]]), ("Tenders received", str(f["number_of_tenders_received"]))]),
            ("Supplier", [("Legal name", sup), ("Registration", f["supplier_registration_number"]), ("Address", s["address"])]),
            ("Dates", [("Award decision", fmt_date(f["award_date"], ds)), ("Entered into", fmt_date(f["contract_signed_date"], ds)),
                       ("Commencement", fmt_date(f["contract_start_date"], ds)), ("Initial expiry", fmt_date(f["contract_end_date"], ds)),
                       ("Extension options", f"{f['extension_options_months']} months (one option)"), ("Maximum term", f"{f['maximum_term_months']} months")]),
            ("Value", [("Annual (ex VAT)", fmt_money(f["contract_value_gbp"] / 3, ms)), ("Estimated total (ex VAT)", fmt_money(f["contract_value_gbp"], ms)),
                       ("Basis", "Schedule of rates with target cost")]),
            ("Terms", [("Payment", f"{f['payment_terms_days']} days from a valid invoice"), ("KPIs", f"{len(f['kpis'])} set (see schedule)")]),
        ]
        story = head + [P("Contract particulars", "h2"), kv_table_borderless_grouped(groups),
                        P("Key performance indicator schedule", "h2"),
                        P("Measured and reported in accordance with section 52. Review dates are printed once for each reporting period.", "small")]
        kpi_rows = []
        periods = [("30-Jun-2026", 0), ("30-Sep-2026", 1), ("31-Dec-2026", 2)]
        targets = ["95%", "90%", "100%"]
        for per, _ in periods:
            for i, k in enumerate(f["kpis"]):
                kpi_rows.append([per if i == 0 else "", f"KPI {i + 1}", k, targets[i]])
        story.append(data_table(["Review date", "Ref", "Indicator", "Target"], kpi_rows, [28 * mm, 16 * mm, width - 28 * mm - 16 * mm - 22 * mm, 22 * mm], grid=False))
        return story

    if st["kv"] == "stacked_two_column":
        left = stacked_kv([
            ("Contracting authority", f["contracting_authority"]),
            ("Notice reference", f["notice_reference"]),
            ("Contract reference", f["contract_reference"]),
            ("Supplier", sup),
            ("Registration number", f["supplier_registration_number"]),
            ("Procedure", corpus.PROCEDURE_LABEL[f["procedure"]]),
            ("CPV codes", fmt_cpv(f["cpv_codes"])),
            ("Date of award", fmt_date(f["award_date"], ds)),
            ("Contract entered into", fmt_date(f["contract_signed_date"], ds)),
            ("Start", fmt_date(f["contract_start_date"], ds)),
            ("End (initial term)", fmt_date(f["contract_end_date"], ds)),
            ("Options", f"{f['extension_options_months']} months"),
            ("Maximum term", f"{f['maximum_term_months']} months"),
            ("Contract value", fmt_money(f["contract_value_gbp"], ms) + " ex VAT"),
            ("Payment terms", f"{f['payment_terms_days']} days"),
            ("Tenders received", str(f["number_of_tenders_received"])),
        ])
        right = [
            P("About this contract", "h2"),
            P(f"{f['contracting_authority']} has entered into a call-off contract under a national framework for a managed ICT service desk and end-user device support service. "
              f"The service covers approximately 2,400 council staff across 31 sites, with a single point of contact for incidents and requests, device imaging and deployment, "
              "and asset management for laptops, tablets and smartphones."),
            P("The contract was awarded following a mini-competition among three framework suppliers. The successful supplier scored highest on the quality criteria, "
              "in particular on its proposed approach to first-time fix and to the transition from the incumbent provider."),
            P("Key performance indicators", "h2"),
        ] + [P(f"{i}. {k}") for i, k in enumerate(f["kpis"], 1)] + [
            P("Performance against each indicator will be reported monthly and published at least annually in a contract performance notice where required by section 71.", "small"),
            P("Payment", "h2"),
            P(f"The Council has agreed payment terms of {f['payment_terms_days']} days from receipt of a valid invoice. Invoices are submitted monthly in arrears against the service charge schedule."),
            P("Publication", "h2"),
            P(f"This notice was published on {fmt_date(f['notice_published_date'], ds)}. The contract was entered into on {fmt_date(f['contract_signed_date'], ds)}."),
        ]
        return head + [FrameBreak()] + left + [FrameBreak()] + right
    raise ValueError(st["kv"])


def story_change_notice(spec: CaseSpec) -> list:
    f, st, c, s = spec.fields, spec.style, spec.council, spec.supplier
    ds, ms = st["date"], st["money"]
    head = [P("Contract change notice", "title"),
            P(f"Procurement Act 2023, section 75 &nbsp;·&nbsp; Notice reference {f['notice_reference']} &nbsp;·&nbsp; Published {fmt_date(f['notice_published_date'], ds)}", "subtitle")]
    rows_a = [("Contracting authority", f["contracting_authority"]), ("Address", c["address"]), ("Contact", c["contact"])]
    rows_o = [
        ("Contract title", f["contract_title"]), ("Contract reference", f["contract_reference"]),
        ("Supplier", s["name"]), ("Supplier registration number", f["supplier_registration_number"]),
        ("Nature of the contract", f["procurement_type"].capitalize()), ("CPV codes", fmt_cpv(f["cpv_codes"])),
        ("Procedure used at award", corpus.PROCEDURE_LABEL[f["procedure"]]),
        ("Date of award decision", fmt_date(f["award_date"], ds)),
        ("Date contract entered into", fmt_date(f["contract_signed_date"], ds)),
        ("Contract start date", fmt_date(f["contract_start_date"], ds)),
        ("Contract end date before modification", fmt_date(f["original_end_date"], ds)),
        ("Extension options at award", f"{f['extension_options_months']} months" if f["extension_options_months"] else "None"),
        ("Maximum term at award (including options)", f"{f['maximum_term_months']} months"),
        ("Contract value before modification (ex VAT)", fmt_money(f["original_contract_value_gbp"], ms)),
    ]
    rows_m = [
        ("Modification number", str(f["modification_number"])),
        ("Date modification takes effect", fmt_date(f["modification_date"], ds)),
        ("Description of the modification", f["modification_description"]),
        ("Ground relied on", corpus.GROUND_LABEL[f["modification_ground"]]),
        ("Change in contract value (ex VAT)", ("+" if f["value_change_gbp"] >= 0 else "-") + fmt_money(abs(f["value_change_gbp"]), ms)
         + (f" ({f['value_change_percent']:+.1f}%)" if st.get("print_percent", True) else "")),
        ("Contract value after modification (ex VAT)", fmt_money(f["revised_contract_value_gbp"], ms)),
        ("Contract end date after modification", fmt_date(f["revised_end_date"], ds)),
        ("Change in term", f"{f['term_change_months']:+d} months" if f["term_change_months"] else "No change"),
        ("Voluntary standstill period", "Yes - eight working days from publication of this notice" if f["voluntary_standstill"] else "No"),
    ]
    story = head + [P("1. Contracting authority", "h2"), kv_table_grid(rows_a),
                    P("2. Contract being modified", "h2"), kv_table_grid(rows_o),
                    P("3. Modification", "h2"), kv_table_grid(rows_m),
                    P("4. Statement", "h2")]
    if f["contract_change_notice_published"]:
        story.append(P("The contracting authority intends to modify the contract as described above. This contract change notice is published before the modification is made, "
                       "in accordance with section 75(1) of the Procurement Act 2023."))
    else:
        story.append(P("No contract change notice was published before this modification was made."))
    if f["modification_ground"] == "s74_below_threshold":
        story.append(P("The authority considers the modification to be a below-threshold modification under section 74(1)(c): it does not materially change the scope of the contract."))
    elif f["modification_ground"] == "sch8_para3_unforeseeable":
        story.append(P("The authority considers that the circumstances giving rise to the modification could not reasonably have been foreseen before the award and that the modification does not change the overall nature of the contract."))
    story += [Spacer(1, 6), P(f"Authorised by {corpus.OFFICERS[spec.fields['modification_number'] % len(corpus.OFFICERS)]}.", "small")]
    return story


def story_contract_document(spec: CaseSpec) -> list:
    f, st, c, s = spec.fields, spec.style, spec.council, spec.supplier
    ds, ms = st["date"], st["money"]
    if f.get("modification_number"):
        return _story_deed_of_variation(spec)
    sup = s["name"] + (f" (trading as {s['ta']})" if s.get("ta") else "")
    story = [P("Contract for the provision of leisure centre operating services", "title"),
             P(f"between {f['contracting_authority']} and {sup}", "subtitle"),
             P(f"Contract reference {f['contract_reference']}", "small"), Spacer(1, 8),
             P("Schedule 1 - Contract particulars", "h2"),
             kv_table_grid([
                 ("The Authority", f["contracting_authority"] + ", " + c["address"]),
                 ("The Supplier", sup + ", company number " + f["supplier_registration_number"] + ", " + s["address"]),
                 ("Services", f["contract_title"] + " comprising the operation, management and maintenance of the Eastmere Leisure Centre, Calder Pool and the Meadow Lane Sports Hub"),
                 ("CPV", fmt_cpv(f["cpv_codes"])),
                 ("Procurement procedure", corpus.PROCEDURE_LABEL[f["procedure"]] + f" ({f['number_of_tenders_received']} tenders received)"),
                 ("Date of award decision", fmt_date(f["award_date"], ds)),
                 ("Date of this Contract", fmt_date(f["contract_signed_date"], ds)),
                 ("Commencement Date", fmt_date(f["contract_start_date"], ds)),
                 ("Expiry Date", fmt_date(f["contract_end_date"], ds)),
                 ("Extension", "There is no option to extend. The Maximum Term is " + str(f["maximum_term_months"]) + " months."),
                 ("Contract Price", fmt_money(f["contract_value_gbp"], ms) + " exclusive of VAT, being the aggregate management fee over the Term"),
                 ("Payment terms", f"{f['payment_terms_days']} days from receipt of a valid invoice (clause 12.3)"),
             ]),
             P("Schedule 2 - Key performance indicators", "h2"),
             P("The Parties have agreed the following key performance indicators for the purposes of section 52 of the Procurement Act 2023:")]
    for i, k in enumerate(f["kpis"], 1):
        story.append(P(f"KPI {i}: {k}"))
    story += [P("The Authority has determined that further indicators are not appropriate for this contract.", "small"),
              P("Execution", "h2"),
              P("This Contract has been executed as a deed and is delivered on the date set out in the Contract particulars."),
              signature_block(corpus.OFFICERS[1], s["name"], fmt_date(f["contract_signed_date"], ds))]
    return story


def _story_deed_of_variation(spec: CaseSpec) -> list:
    f, st, c, s = spec.fields, spec.style, spec.council, spec.supplier
    ds, ms = st["date"], st["money"]
    story = [
        P("Officer decision record", "title"),
        P(f"{f['contracting_authority']} &nbsp;·&nbsp; Parks and Open Spaces &nbsp;·&nbsp; Decision ref ODR/2026/0142", "subtitle"),
        P("Decision", "h2"),
        P(f"To enter into Deed of Variation No. {f['modification_number']} to contract {f['contract_reference']} ({f['contract_title']}) with {s['name']} "
          f"(company number {f['supplier_registration_number']}), extending the term by {f['term_change_months']} months to {fmt_date(f['revised_end_date'], ds)} and increasing the "
          f"contract sum from {fmt_money(f['original_contract_value_gbp'], ms)} to {fmt_money(f['revised_contract_value_gbp'], ms)} (an increase of {fmt_money(f['value_change_gbp'], ms)}) "
          f"to reflect the additional 14 hectares of open space transferred from Harrowdale Parish Council."),
        P("Background", "h2"),
        P(f"The contract was awarded following an open procedure on {fmt_date(f['award_date'], ds)} and entered into on {fmt_date(f['contract_signed_date'], ds)} for an initial term of "
          f"{f['maximum_term_months']} months from {fmt_date(f['contract_start_date'], ds)} with no option to extend. The contract therefore expired on {fmt_date(f['original_end_date'], ds)}. "
          "The supplier has continued to deliver the service since that date on the existing terms pending re-procurement, which has been delayed by the service review."),
        P("Procurement advice", "h2"),
        P("The Head of Procurement advises that the variation is treated as a non-substantial modification under section 74(1)(b) of the Procurement Act 2023 on the basis that "
          "the overall nature of the contract is unchanged. No contract change notice was published before the variation was agreed. A contract change notice will be considered "
          "once the re-procurement timetable is confirmed."),
        P("Decision taken", "h2"),
        P(f"Decision taken by {corpus.OFFICERS[4]} on {fmt_date(f['modification_date'], ds)}."),
        PageBreak(),
        P(f"Deed of Variation No. {f['modification_number']}", "title"),
        P(f"to the contract dated {fmt_date(f['contract_signed_date'], ds)} between (1) {f['contracting_authority']} and (2) {s['name']}", "subtitle"),
        P("Recitals", "h2"),
        P(f"(A) By a contract dated {fmt_date(f['contract_signed_date'], ds)} (the Contract) the Authority engaged the Supplier to provide {f['contract_title'].lower()} "
          f"for a term commencing on {fmt_date(f['contract_start_date'], ds)} and expiring on {fmt_date(f['original_end_date'], ds)} for a contract sum of {fmt_money(f['original_contract_value_gbp'], ms)} excluding VAT."),
        P(f"(B) The Parties wish to extend the term of the Contract and to vary the contract sum on the terms set out in this Deed, which is dated {fmt_date(f['modification_date'], ds)}."),
        P("Operative provisions", "h2"),
        P(f"1. Clause 3.1 of the Contract is amended so that the Expiry Date is {fmt_date(f['revised_end_date'], ds)}."),
        P(f"2. Schedule 4 (Pricing) is amended so that the contract sum is {fmt_money(f['revised_contract_value_gbp'], ms)} excluding VAT, the additional sum of "
          f"{fmt_money(f['value_change_gbp'], ms)} being attributable to the additional open space described in the Annex."),
        P("3. The Parties confirm that the Contract continued in effect between its original Expiry Date and the date of this Deed and ratify the services delivered in that period."),
        P("4. Save as varied by this Deed the Contract continues in full force and effect."),
        Spacer(1, 10),
        signature_block(corpus.OFFICERS[4], s["name"], fmt_date(f["modification_date"], ds)),
    ]
    return story


BUILDERS = {
    "contract_award_notice": story_award_notice,
    "contract_details_notice": story_contract_details_notice,
    "contract_change_notice": story_change_notice,
    "contract_document": story_contract_document,
}

DOC_LABEL = {
    "contract_award_notice": "Contract award notice",
    "contract_details_notice": "Contract details notice",
    "contract_change_notice": "Contract change notice",
    "contract_document": "Contract document",
}


# ====================================================================== output
def write_case(spec: CaseSpec, out_dir: str) -> dict:
    case_dir = os.path.join(out_dir, spec.case_id)
    os.makedirs(case_dir, exist_ok=True)
    pdf_path = os.path.join(case_dir, "document.pdf")
    story = BUILDERS[spec.fields["notice_type"]](spec)
    label = DOC_LABEL[spec.fields["notice_type"]]
    tmp_pdf = pdf_path + ".text.pdf" if spec.scanned else pdf_path
    build_pdf(tmp_pdf, story, spec.fields["contracting_authority"], label,
              two_column=(spec.style.get("kv") == "stacked_two_column"), footer_style=spec.style.get("footer", "standard"),
              header=spec.extra.get("header"))
    if spec.scanned:
        rasterise_to_scanned_pdf(tmp_pdf, pdf_path, seed=int(spec.case_id[-3:]))
        os.remove(tmp_pdf)
    pages = pdf_page_count(pdf_path)

    flags = pa2023.expected_flags(spec.fields)
    with open(pdf_path, "rb") as fh:
        sha = hashlib.sha256(fh.read()).hexdigest()
    gold = {
        "case_id": spec.case_id,
        "slug": spec.slug,
        "title": spec.title,
        "document": "document.pdf",
        "pages": pages,
        "sha256": sha,
        "hazards": spec.hazards,
        "notes": spec.notes,
        "fields": spec.fields,
        "expected_flags": flags,
        "provenance": {"source": "synthetic", "generator": f"crtest.gen {GENERATOR_VERSION}", "licence": "CC0-1.0",
                       "statement": "Fictional authority, supplier and figures. Generated for extraction testing; not a real notice."},
    }
    with open(os.path.join(case_dir, "gold.json"), "w", encoding="utf-8") as fh:
        json.dump(gold, fh, indent=2, ensure_ascii=False)
    with open(os.path.join(case_dir, "expected.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["field", "value"])
        for k in FIELD_NAMES:
            v = spec.fields[k]
            if k == "lots":
                continue
            w.writerow([k, json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else ("" if v is None else v)])
    if spec.fields["lots"]:
        with open(os.path.join(case_dir, "expected_lots.csv"), "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["lot_number", "lot_title", "supplier_name", "value_gbp", "awarded"])
            w.writeheader()
            for lot in spec.fields["lots"]:
                w.writerow(lot)
    with open(os.path.join(case_dir, "notes.md"), "w", encoding="utf-8") as fh:
        fh.write(f"# {spec.case_id} - {spec.title}\n\n{spec.notes}\n\n## Hazards\n\n")
        for h in spec.hazards:
            fh.write(f"- **{h}**: {HAZARD_DESCRIPTIONS.get(h, '')}\n")
        fh.write("\n## Expected flags\n\n")
        if flags:
            for fl in flags:
                fh.write(f"- `{fl['id']}` ({fl['severity']}): {fl['detail']}\n")
        else:
            fh.write("- none\n")
    return {"case_id": spec.case_id, "slug": spec.slug, "title": spec.title, "notice_type": spec.fields["notice_type"],
            "pages": pages, "hazards": spec.hazards, "expected_flags": [x["id"] for x in flags], "sha256": sha,
            "document": f"{spec.case_id}/document.pdf", "gold": f"{spec.case_id}/gold.json"}


def render_page_images(case_dir: str, dpi: int = 70) -> int:
    pages_dir = os.path.join(case_dir, "pages")
    if os.path.isdir(pages_dir):
        shutil.rmtree(pages_dir)
    os.makedirs(pages_dir)
    subprocess.run(["pdftoppm", "-r", str(dpi), "-png", os.path.join(case_dir, "document.pdf"), os.path.join(pages_dir, "page")], check=True)
    names = sorted(os.listdir(pages_dir))
    for i, n in enumerate(names, 1):
        os.rename(os.path.join(pages_dir, n), os.path.join(pages_dir, f"page-{i}.png"))
    return len(names)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Generate the synthetic test set")
    ap.add_argument("--out", default="cases")
    ap.add_argument("--only", help="comma-separated case ids")
    ap.add_argument("--no-images", action="store_true", help="skip page PNGs for the site")
    args = ap.parse_args(argv)
    require_tools("pdftoppm")
    specs = build_specs()
    if args.only:
        keep = set(args.only.split(","))
        specs = [s for s in specs if s.case_id in keep]
    os.makedirs(args.out, exist_ok=True)
    manifest = {"schema_version": SCHEMA["$schema_version"], "generator_version": GENERATOR_VERSION,
                "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "cases": []}
    for spec in specs:
        entry = write_case(spec, args.out)
        if not args.no_images:
            render_page_images(os.path.join(args.out, spec.case_id))
        manifest["cases"].append(entry)
        print(f"{spec.case_id}  {entry['pages']}p  {spec.slug}  flags={entry['expected_flags']}")
    if not args.only:
        with open(os.path.join(args.out, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)
        with open(os.path.join(args.out, "hazards.json"), "w", encoding="utf-8") as fh:
            json.dump(HAZARD_DESCRIPTIONS, fh, indent=2)
    print(f"wrote {len(specs)} cases to {args.out}")


if __name__ == "__main__":
    main()
