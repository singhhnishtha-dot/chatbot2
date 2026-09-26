#!/usr/bin/env python3
"""
Builds submission.jsonl (challenge-brief.md §7.2) by calling the exact same
`compose()` function that bot.py runs live — so the JSONL is guaranteed
consistent with what the HTTP bot would actually return.

Usage:
    # 1. Expand the seed dataset (deterministic, same for every candidate):
    python ../dataset/generate_dataset.py --seed-dir ../dataset --out ./expanded

    # 2. Generate the submission file from the 30 canonical test pairs:
    python generate_submission.py --dataset ./expanded --out ./submission.jsonl
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from bot import compose


def load_dataset(dataset_dir: Path):
    categories = {}
    for f in (dataset_dir / "categories").glob("*.json"):
        d = json.load(open(f, encoding="utf-8"))
        categories[d["slug"]] = d

    merchants = {}
    for f in (dataset_dir / "merchants").glob("*.json"):
        d = json.load(open(f, encoding="utf-8"))
        merchants[d["merchant_id"]] = d

    customers = {}
    for f in (dataset_dir / "customers").glob("*.json"):
        d = json.load(open(f, encoding="utf-8"))
        customers[d["customer_id"]] = d

    triggers = {}
    for f in (dataset_dir / "triggers").glob("*.json"):
        d = json.load(open(f, encoding="utf-8"))
        triggers[d["id"]] = d

    return categories, merchants, customers, triggers


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="./expanded", help="Expanded dataset dir (has categories/merchants/customers/triggers/test_pairs.json)")
    ap.add_argument("--out", default="./submission.jsonl")
    args = ap.parse_args()

    dataset_dir = Path(args.dataset).resolve()
    categories, merchants, customers, triggers = load_dataset(dataset_dir)
    pairs = json.load(open(dataset_dir / "test_pairs.json", encoding="utf-8"))["pairs"]

    lines = []
    for pair in pairs:
        test_id = pair["test_id"]
        trigger = triggers.get(pair["trigger_id"])
        merchant = merchants.get(pair["merchant_id"])
        customer = customers.get(pair["customer_id"]) if pair.get("customer_id") else None
        if not trigger or not merchant:
            print(f"[WARN] {test_id}: missing trigger or merchant, skipping")
            continue
        category = categories.get(merchant.get("category_slug", ""))
        result = compose(category, merchant, trigger, customer)
        lines.append({"test_id": test_id, **result})

    with open(args.out, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")

    print(f"Wrote {len(lines)} lines to {args.out}")


if __name__ == "__main__":
    main()
