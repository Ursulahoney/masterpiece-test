#!/usr/bin/env python3
"""
Medical Bill Translation Script
Processes structured medical billing line-item data against a code definitions
pack and produces a patient-facing Markdown summary following the exact layout
and rules defined in rules_BIG.md.
"""

import csv
import io
import json
import os
import re
import sys
from datetime import date, datetime
from collections import OrderedDict


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_date(s: str) -> date:
    """Parse YYYY-MM-DD string to a date object."""
    return datetime.strptime(s.strip(), "%Y-%m-%d").date()


def parse_charge(s: str) -> float:
    """Parse a charge string to float, stripping $ and commas."""
    return float(s.strip().replace("$", "").replace(",", ""))


def parse_units(s: str) -> int:
    return int(s.strip())


# ---------------------------------------------------------------------------
# Load code definitions pack from CSV file
# ---------------------------------------------------------------------------

def load_code_definitions(path: str) -> dict:
    """
    Returns dict keyed by code string.
    Value: dict with keys code_type, official_description, plain_english,
           status, effective_date (date object).
    """
    defs = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            code = row["code"].strip()
            defs[code] = {
                "code_type": row["code_type"].strip(),
                "official_description": row["official_description"].strip(),
                "plain_english": row["plain_english"].strip(),
                "status": row["status"].strip(),
                "effective_date": parse_date(row["effective_date"]),
            }
    return defs


# ---------------------------------------------------------------------------
# Parse line items from pasted CSV text
# ---------------------------------------------------------------------------

def parse_line_items(text: str) -> list[dict]:
    """
    Accepts the raw pasted text (CSV with header) and returns a list of dicts
    sorted by line_id ascending.
    """
    text = text.strip()
    if not text:
        return []

    reader = csv.DictReader(io.StringIO(text))
    items = []
    for row in reader:
        try:
            items.append({
                "line_id": int(row["line_id"].strip()),
                "date_of_service": row["date_of_service"].strip(),
                "dos_date": parse_date(row["date_of_service"]),
                "code": row["code"].strip(),
                "code_type": row["code_type"].strip(),
                "units": parse_units(row["units"]),
                "charge": parse_charge(row["charge"]),
                "bill_label": row.get("bill_label", "").strip(),
            })
        except (KeyError, ValueError) as exc:
            print(f"::warning::Skipping malformed row: {row} – {exc}",
                  file=sys.stderr)
    items.sort(key=lambda r: r["line_id"])
    return items


# ---------------------------------------------------------------------------
# Rule engine
# ---------------------------------------------------------------------------

def evaluate_line(item: dict, code_defs: dict) -> dict:
    """
    For a single line item, return an enriched dict that includes:
      official_description, plain_english, notes, needs_clarification (bool),
      clarification_reasons (list of strings).
    """
    code = item["code"]
    bill_code_type = item["code_type"]
    dos = item["dos_date"]
    units = item["units"]
    charge = item["charge"]

    official = ""
    plain = ""
    notes = ""
    needs_clar = False
    clar_reasons = []

    # --- Rule 3: code not in pack -----------------------------------------
    if code not in code_defs:
        official = "Definition not provided in Code Pack."
        plain = "Definition not provided in Code Pack."
        needs_clar = True
        clar_reasons.append("Missing code definition")
    else:
        cd = code_defs[code]
        pack_code_type = cd["code_type"]

        # --- Rule 6: code_type mismatch -----------------------------------
        if bill_code_type != pack_code_type:
            official = "Definition not provided in Code Pack."
            plain = "Definition not provided in Code Pack."
            notes = "code_type mismatch (bill: {}, pack: {})".format(
                bill_code_type, pack_code_type)
            needs_clar = True
            clar_reasons.append("code_type mismatch")

        # --- Rule 4 / Rule 5: status & effective_date ---------------------
        elif cd["status"] != "Active" or cd["effective_date"] > dos:
            official = "Definition not provided in Code Pack."
            plain = "Definition not provided in Code Pack."
            notes = "Code is inactive or not effective for the date of service."
            needs_clar = True
            clar_reasons.append("Code inactive/not effective")

        else:
            # Code is valid
            official = cd["official_description"]
            plain = cd["plain_english"]

    # --- Clarification trigger: units = 0 AND charge > 0 ------------------
    if units == 0 and charge > 0:
        needs_clar = True
        clar_reasons.append("units = 0 with charge > 0")
        if not notes:
            notes = "units = 0 but charge > 0"
        else:
            notes += "; units = 0 but charge > 0"

    # --- Clarification trigger: code_type MOD and charge > 0 ---------------
    if bill_code_type == "MOD" and charge > 0:
        needs_clar = True
        clar_reasons.append("Modifier with charge > 0")
        if not notes:
            notes = "Modifier line has a charge > $0"
        else:
            notes += "; Modifier line has a charge > $0"

    return {
        **item,
        "official_description": official,
        "plain_english": plain,
        "notes": notes,
        "needs_clarification": needs_clar,
        "clarification_reasons": clar_reasons,
    }


# ---------------------------------------------------------------------------
# Duplicate detection  (Rule 7-10)
# ---------------------------------------------------------------------------

def find_duplicates(items: list[dict]) -> list[dict]:
    """
    Returns a list of duplicate group dicts, each with:
      group_id, line_ids, matching_fields.
    A duplicate group has 2+ items sharing date_of_service + code + units + charge.
    """
    from collections import defaultdict
    buckets = defaultdict(list)
    for it in items:
        key = (it["date_of_service"], it["code"], it["units"], it["charge"])
        buckets[key].append(it["line_id"])

    groups = []
    gid = 1
    for key, lids in buckets.items():
        if len(lids) >= 2:
            dos, code, units, charge = key
            groups.append({
                "group_id": gid,
                "line_ids": sorted(lids),
                "dos": dos,
                "code": code,
                "units": units,
                "charge": charge,
            })
            gid += 1
    # Sort groups by earliest line_id in each group
    groups.sort(key=lambda g: g["line_ids"][0])
    # Re-number after sort
    for idx, g in enumerate(groups, 1):
        g["group_id"] = idx
    return groups


# ---------------------------------------------------------------------------
# Build output sections
# ---------------------------------------------------------------------------

def build_section1(header: dict) -> str:
    lines = ["SECTION 1: Bill Header Summary", ""]
    lines.append(f"Patient Name: {header.get('patient_name', 'N/A')}")
    lines.append(f"Account Number: {header.get('account_number', 'N/A')}")
    lines.append(f"Facility / Provider: {header.get('facility', 'N/A')}")
    lines.append(f"Statement Date: {header.get('statement_date', 'N/A')}")
    lines.append(f"Total Billed: ${header.get('total_billed', 'N/A')}")
    return "\n".join(lines)


def build_section2(evaluated: list[dict]) -> str:
    lines = ["SECTION 2: Plain-English Line Item Table", ""]
    hdr = ("| Line # | DOS | Code | Code Type | Official Description "
           "| Plain-English | Units | Charge | Notes |")
    sep = ("|---|---|---|---|---|---|---|---|---|")
    lines.append(hdr)
    lines.append(sep)
    for e in evaluated:
        charge_str = f"${e['charge']:,.2f}"
        row = (
            f"| {e['line_id']} "
            f"| {e['date_of_service']} "
            f"| {e['code']} "
            f"| {e['code_type']} "
            f"| {e['official_description']} "
            f"| {e['plain_english']} "
            f"| {e['units']} "
            f"| {charge_str} "
            f"| {e['notes']} |"
        )
        lines.append(row)
    return "\n".join(lines)


def build_section3(dup_groups: list[dict]) -> str:
    lines = ["SECTION 3: Duplicates Table", ""]
    if not dup_groups:
        lines.append(
            "No duplicates found under the project\u2019s duplicate rule."
        )
        return "\n".join(lines)
    hdr = ("| Duplicate Group | Line #s | Matching Fields "
           "| Suggested question for billing |")
    sep = "|---|---|---|---|"
    lines.append(hdr)
    lines.append(sep)
    for g in dup_groups:
        lid_str = ", ".join(str(l) for l in g["line_ids"])
        mf = (f"DOS={g['dos']}, Code={g['code']}, "
              f"Units={g['units']}, Charge=${g['charge']:,.2f}")
        question = ("This appears duplicated under the project\u2019s "
                    "duplicate rule. Please confirm with billing.")
        row = f"| {g['group_id']} | {lid_str} | {mf} | {question} |"
        lines.append(row)
    return "\n".join(lines)


def build_section4(evaluated: list[dict]) -> str:
    lines = ["SECTION 4: Needs Clarification List", ""]
    clar_items = [e for e in evaluated if e["needs_clarification"]]
    if not clar_items:
        lines.append("No items require clarification.")
        return "\n".join(lines)
    for e in clar_items:
        reasons = "; ".join(e["clarification_reasons"])
        bullet = (
            f"Clarify: Line {e['line_id']} "
            f"(Code {e['code']}, DOS {e['date_of_service']}) \u2013 "
            f"{reasons}."
        )
        lines.append(f"- {bullet}")
    return "\n".join(lines)


def build_section5(evaluated: list[dict], dup_groups: list[dict],
                    header: dict) -> str:
    lines = ["SECTION 5: Patient-Friendly Summary Paragraph", ""]

    total_lines = len(evaluated)
    dos_set = sorted(set(e["date_of_service"] for e in evaluated))
    total_charge = sum(e["charge"] for e in evaluated)
    dup_count = len(dup_groups)
    clar_count = sum(1 for e in evaluated if e["needs_clarification"])
    proc_count = sum(1 for e in evaluated if e["code_type"] == "PROC")
    drug_count = sum(1 for e in evaluated if e["code_type"] == "DRUG")
    rev_count = sum(1 for e in evaluated if e["code_type"] == "REV")
    mod_count = sum(1 for e in evaluated if e["code_type"] == "MOD")

    sentences = []
    sentences.append(
        f"This bill contains {total_lines} line items spanning "
        f"{len(dos_set)} date(s) of service from {dos_set[0]} to "
        f"{dos_set[-1]}."
    )
    sentences.append(
        f"The total billed amount across all line items is "
        f"${total_charge:,.2f}."
    )
    sentences.append(
        f"Services include {proc_count} procedure charge(s), "
        f"{drug_count} medication charge(s), {rev_count} facility/revenue "
        f"charge(s), and {mod_count} modifier line(s)."
    )
    if dup_count > 0:
        sentences.append(
            f"The review identified {dup_count} group(s) of potentially "
            f"duplicated charges based on matching date of service, code, "
            f"units, and charge amount."
        )
        sentences.append(
            "You may wish to ask your billing department to confirm whether "
            "each duplicated charge was intentional."
        )
    else:
        sentences.append(
            "No duplicate charges were identified under the project's "
            "duplicate detection rule."
        )
    if clar_count > 0:
        sentences.append(
            f"Additionally, {clar_count} line item(s) were flagged for "
            f"clarification due to missing definitions, inactive codes, "
            f"code-type mismatches, or unusual unit and charge combinations."
        )
    sentences.append(
        "This summary is provided for informational purposes to help you "
        "understand the charges on your bill."
    )
    sentences.append(
        "It does not constitute medical or financial advice."
    )
    sentences.append(
        "If you have questions about any charges, please contact your "
        "provider's billing office."
    )

    para = " ".join(sentences)
    lines.append(para)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def run(header: dict, line_items_text: str, code_defs_path: str) -> str:
    """Return the full Markdown output string."""
    # Load code definitions
    if not os.path.isfile(code_defs_path):
        return ("**Error:** Code definitions file not found at "
                f"`{code_defs_path}`.")
    code_defs = load_code_definitions(code_defs_path)

    # Parse line items
    items = parse_line_items(line_items_text)
    if not items:
        return ("**Error:** No valid line items could be parsed from the "
                "input. Please verify the pasted data includes the CSV "
                "header row and at least one data row.")

    # Evaluate each line item
    evaluated = [evaluate_line(it, code_defs) for it in items]

    # Detect duplicates
    dup_groups = find_duplicates(items)

    # Compute total for header if not provided
    if not header.get("total_billed") or header["total_billed"] == "N/A":
        header["total_billed"] = f"{sum(e['charge'] for e in evaluated):,.2f}"

    # Build sections
    s1 = build_section1(header)
    s2 = build_section2(evaluated)
    s3 = build_section3(dup_groups)
    s4 = build_section4(evaluated)
    s5 = build_section5(evaluated, dup_groups, header)

    output = "\n\n".join([s1, s2, s3, s4, s5])
    return output


# ---------------------------------------------------------------------------
# CLI / GitHub Actions entry point
# ---------------------------------------------------------------------------

def parse_issue_body(body: str) -> tuple[dict, str]:
    """
    Parse a GitHub Issue body produced by the Issue Form.
    Returns (header_dict, line_items_csv_text).
    The form uses ### headings for each field.
    """
    sections: dict[str, str] = {}
    current_key = None
    current_lines = []

    for line in body.splitlines():
        if line.startswith("### "):
            if current_key is not None:
                sections[current_key] = "\n".join(current_lines).strip()
            current_key = line[4:].strip()
            current_lines = []
        else:
            current_lines.append(line)
    if current_key is not None:
        sections[current_key] = "\n".join(current_lines).strip()

    header = {
        "patient_name": sections.get("Patient Name", "N/A"),
        "account_number": sections.get("Account Number", "N/A"),
        "facility": sections.get("Facility / Provider", "N/A"),
        "statement_date": sections.get("Statement Date", "N/A"),
        "total_billed": sections.get("Total Billed", "N/A"),
    }

    line_items_text = sections.get("Line Items (CSV)", "")
    # Strip markdown code fences if present
    line_items_text = re.sub(r"^```[a-z]*\n?", "", line_items_text)
    line_items_text = re.sub(r"\n?```$", "", line_items_text)

    return header, line_items_text


def main():
    issue_body = os.environ.get("ISSUE_BODY", "")
    code_defs_path = os.environ.get(
        "CODE_DEFS_PATH", "data/code_definitions_pack_BIG.csv"
    )

    if not issue_body.strip():
        print("::error::ISSUE_BODY environment variable is empty or missing.")
        sys.exit(1)

    header, line_items_text = parse_issue_body(issue_body)

    if not line_items_text.strip():
        result = ("**Error:** The Line Items field appears to be empty or "
                  "could not be parsed. Please ensure you pasted the CSV "
                  "data including the header row.")
    else:
        result = run(header, line_items_text, code_defs_path)

    # Write output for GitHub Actions
    output_path = os.environ.get("GITHUB_OUTPUT", "")
    # Write result to a file for the workflow to pick up
    result_file = os.environ.get("RESULT_FILE", "result.md")
    with open(result_file, "w", encoding="utf-8") as f:
        f.write(result)

    print("Translation complete. Output written to", result_file)


if __name__ == "__main__":
    main()
