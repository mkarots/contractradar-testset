import json
import os
import subprocess
import sys

import pytest

from crtest import normalise, score, sysdeps

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_parse_money_formats():
    assert normalise.parse_money("£1,234,567.00") == 1234567.0
    assert normalise.parse_money("£2.31m ex VAT") == 2310000.0
    assert normalise.parse_money("£4 200 000") == 4200000.0
    assert normalise.parse_money("6,100,000.00 GBP") == 6100000.0
    assert normalise.parse_money("seven million nine hundred and eighty thousand pounds (£7,980,000)") == 7980000.0


def test_parse_date_formats():
    for s in ("3 March 2026", "3rd March 2026", "03/03/2026", "2026-03-03", "03-Mar-2026"):
        assert normalise.parse_date(s) == "2026-03-03", s


def test_org_name_normalisation():
    assert normalise.text_similarity("Brightwater Environmental Services Limited", "Brightwater Environmental Services Ltd (trading as Brightwater)") >= 0.92


def test_gold_scores_100_percent(tmp_path):
    cases = os.path.join(ROOT, "cases")
    with open(os.path.join(cases, "manifest.json")) as fh:
        manifest = json.load(fh)
    pred = {"extractor": "gold-echo", "cases": {}}
    for c in manifest["cases"]:
        with open(os.path.join(cases, c["gold"])) as fh:
            g = json.load(fh)
        pred["cases"][c["case_id"]] = {"fields": g["fields"], "flags": [f["id"] for f in g["expected_flags"]]}
    p = tmp_path / "gold-echo.json"
    p.write_text(json.dumps(pred))
    r = score.score(cases, str(p))
    assert r["summary"]["field_accuracy"] == 1.0
    assert r["summary"]["cases_passed"] == len(manifest["cases"])
    assert r["summary"]["flags"]["precision"] == 1.0 and r["summary"]["flags"]["recall"] == 1.0


def test_naive_baseline_passes_clean_case():
    missing = sysdeps.probe("pdftotext")
    if missing:
        pytest.skip(missing)
    r = subprocess.run([sys.executable, "-m", "crtest.extract_naive", "--cases", os.path.join(ROOT, "cases"), "--out", "/tmp/_naive_test.json"], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    s = score.score(os.path.join(ROOT, "cases"), "/tmp/_naive_test.json")
    assert s["per_case"]["CR-001"]["critical_pass"] is True
    assert s["per_case"]["CR-005"]["field_accuracy"] == 0.0  # scanned: no text layer
