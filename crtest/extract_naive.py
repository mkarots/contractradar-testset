"""Deliberately naive baseline: pdftotext -layout plus label regexes.

This is the "wrong extraction" the pilot thread asked for. It reads a
single-column key/value table reasonably well and falls over on page
breaks, borderless or grouped tables, two-column layouts, prose, scans,
and unusual money/date formats. Do not improve it: its job is to show
where a text-dump approach breaks.

Run:  python -m crtest.extract_naive --cases cases --out results/naive_text.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess

from .schema import FIELD_NAMES
from .normalise import parse_date, parse_money, to_int
from .sysdeps import require_tools

NAME = "naive_text"
VERSION = "0.1.0"

# label -> field. Only the "standard" labels: that is the point.
LABELS = {
    "contracting authority": "contracting_authority",
    "supplier": "supplier_name",
    "supplier registration number": "supplier_registration_number",
    "registration number": "supplier_registration_number",
    "contract title": "contract_title",
    "title": "contract_title",
    "contract reference": "contract_reference",
    "cpv codes": "cpv_codes",
    "procedure": "procedure",
    "procedure used at award": "procedure",
    "tenders received": "number_of_tenders_received",
    "date of award decision": "award_date",
    "date of award": "award_date",
    "date contract entered into": "contract_signed_date",
    "contract start date": "contract_start_date",
    "contract end date": "contract_end_date",
    "extension options": "extension_options_months",
    "extension options at award": "extension_options_months",
    "maximum term (including options)": "maximum_term_months",
    "maximum term at award (including options)": "maximum_term_months",
    "contract value (excluding vat)": "contract_value_gbp",
    "total contract value (excluding vat)": "contract_value_gbp",
    "contract sum (excluding vat)": "contract_value_gbp",
    "payment terms": "payment_terms_days",
    "modification number": "modification_number",
    "date modification takes effect": "modification_date",
    "description of the modification": "modification_description",
    "ground relied on": "modification_ground",
    "change in contract value (ex vat)": "value_change_gbp",
    "contract value before modification (ex vat)": "original_contract_value_gbp",
    "contract value after modification (ex vat)": "revised_contract_value_gbp",
    "contract end date before modification": "original_end_date",
    "contract end date after modification": "revised_end_date",
    "change in term": "term_change_months",
    "voluntary standstill period": "voluntary_standstill",
    "nature of the contract": "procurement_type",
}

PROCEDURE = {"open procedure": "open", "competitive flexible procedure": "competitive_flexible", "direct award": "direct_award", "call-off from a framework": "framework_call_off"}
GROUNDS = {"paragraph 1": "sch8_para1_provided_for", "paragraph 2": "sch8_para2_urgency", "paragraph 3": "sch8_para3_unforeseeable", "paragraph 4": "sch8_para4_known_risk",
           "paragraph 5": "sch8_para5_additional", "paragraph 6": "sch8_para6_restructuring", "74(1)(b)": "s74_non_substantial", "74(1)(c)": "s74_below_threshold"}

DATE_FIELDS = {"award_date", "contract_signed_date", "contract_start_date", "contract_end_date", "modification_date", "original_end_date", "revised_end_date", "notice_published_date", "standstill_end_date"}
MONEY_FIELDS = {"contract_value_gbp", "value_change_gbp", "original_contract_value_gbp", "revised_contract_value_gbp"}
INT_FIELDS = {"number_of_tenders_received", "extension_options_months", "maximum_term_months", "payment_terms_days", "modification_number", "term_change_months"}


def pdf_text(path: str) -> str:
    return subprocess.run(["pdftotext", "-layout", path, "-"], check=True, capture_output=True, text=True).stdout


def naive_money(s: str) -> float | None:
    """Only understands £1,234,567.00 style. That is the naive part."""
    m = re.search(r"£(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{2}))?", s)
    if not m:
        return None
    return float(m.group(1).replace(",", "") + ("." + m.group(2) if m.group(2) else ""))


def naive_date(s: str) -> str | None:
    """Only long-form (3 March 2026) and DD/MM/YYYY."""
    m = re.search(r"\b(\d{1,2}) ([A-Z][a-z]+) (\d{4})\b", s)
    if m:
        return parse_date(m.group(0))
    m = re.search(r"\b(\d{2})/(\d{2})/(\d{4})\b", s)
    if m:
        return parse_date(m.group(0))
    return None


def extract(text: str) -> dict:
    fields = {k: None for k in FIELD_NAMES}
    fields["lots"] = []
    fields["cpv_codes"] = None
    fields["kpis"] = []
    lines = text.splitlines()
    first = text.strip().splitlines()[0].strip().lower() if text.strip() else ""
    if first.startswith("contract award notice"):
        fields["notice_type"] = "contract_award_notice"
    elif first.startswith("contract details notice"):
        fields["notice_type"] = "contract_details_notice"
    elif first.startswith("contract change notice"):
        fields["notice_type"] = "contract_change_notice"
    elif text.strip():
        fields["notice_type"] = "contract_document"
    m = re.search(r"Notice reference (\S+)", text)
    if m:
        fields["notice_reference"] = m.group(1)
    m = re.search(r"Published (\d{1,2} [A-Z][a-z]+ \d{4}|\d{2}/\d{2}/\d{4})", text)
    if m:
        fields["notice_published_date"] = naive_date(m.group(1))
    m = re.search(r"standstill period[^.]*ends on (\d{1,2} [A-Z][a-z]+ \d{4}|\d{2}/\d{2}/\d{4})", text, re.I)
    if m:
        fields["standstill_end_date"] = naive_date(m.group(1))

    for line in lines:
        # "Label   value" with at least two spaces between
        m = re.match(r"^\s*([A-Za-z][A-Za-z ()/-]+?)\s{2,}(\S.*)$", line)
        if not m:
            continue
        label = m.group(1).strip().lower()
        value = m.group(2).strip()
        field_name = LABELS.get(label)
        if not field_name or fields.get(field_name) not in (None, [], ""):
            continue
        if field_name in DATE_FIELDS:
            fields[field_name] = naive_date(value)
        elif field_name in MONEY_FIELDS:
            if field_name == "contract_value_gbp" and "excluding vat" in label:
                fields["value_includes_vat"] = False
            v = naive_money(value)
            if v is not None and value.strip().startswith("-"):
                v = -v
            fields[field_name] = v
            if field_name == "value_change_gbp":
                pm = re.search(r"\(([+-]?\d+(?:\.\d)?)%\)", value)
                fields["value_change_percent"] = float(pm.group(1)) if pm else None
        elif field_name in INT_FIELDS:
            if field_name == "extension_options_months" and value.lower().startswith("none"):
                fields[field_name] = 0
            elif field_name == "term_change_months" and value.lower().startswith("no change"):
                fields[field_name] = 0
            else:
                fields[field_name] = to_int(value)
        elif field_name == "cpv_codes":
            fields[field_name] = re.findall(r"\b\d{8}\b", value)
        elif field_name == "procedure":
            fields[field_name] = next((v for k, v in PROCEDURE.items() if k in value.lower()), None)
        elif field_name == "modification_ground":
            fields[field_name] = next((v for k, v in GROUNDS.items() if k in value), None)
        elif field_name == "procurement_type":
            fields[field_name] = value.split()[0].lower() if value.split()[0].lower() in ("goods", "services", "works") else None
            fields["light_touch"] = "light touch" in value.lower()
        elif field_name == "voluntary_standstill":
            fields[field_name] = value.lower().startswith("yes")
        else:
            fields[field_name] = value
    fields["kpis"] = [m.group(1).strip() for m in re.finditer(r"^\s*KPI \d+: (.+)$", text, re.M)]
    if fields["notice_type"] == "contract_change_notice":
        fields["contract_change_notice_published"] = bool(re.search(r"published\s+before\s+the\s+modification", text))
    if fields["notice_type"] in ("contract_award_notice", "contract_details_notice", "contract_change_notice"):
        fields["contracting_authority_type"] = "sub_central"
    if fields.get("supplier_registration_number"):
        fields["supplier_registration_number"] = fields["supplier_registration_number"].split()[0]
    return fields


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default="cases")
    ap.add_argument("--out", default="results/naive_text.json")
    args = ap.parse_args(argv)
    require_tools("pdftotext")
    with open(os.path.join(args.cases, "manifest.json"), encoding="utf-8") as fh:
        manifest = json.load(fh)
    out = {"extractor": NAME, "version": VERSION, "run_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "description": "pdftotext -layout + label regexes. Deliberately simple baseline.", "cases": {}}
    for c in manifest["cases"]:
        text = pdf_text(os.path.join(args.cases, c["document"]))
        out["cases"][c["case_id"]] = {"fields": extract(text), "flags": [], "text_chars": len(text.strip())}
        print(c["case_id"], "chars", len(text.strip()))
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, ensure_ascii=False)
    print("wrote", args.out)


if __name__ == "__main__":
    main()
