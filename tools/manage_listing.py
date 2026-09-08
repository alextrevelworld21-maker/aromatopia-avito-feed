#!/usr/bin/env python3
"""Guarded mutations for the Aromatopia Avito autoload feed."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import re
from pathlib import Path
from xml.etree import ElementTree as ET


ALLOWED_IDS = {
    "aromatopia-candle-mandarin-coriander-60",
    "aromatopia-candle-wild-berries-honey-60",
    "aromatopia-candle-coconut-mango-60",
    "aromatopia-diffuser-mandarin-coriander-150",
    "aromatopia-diffuser-wild-berries-honey-50",
}

CONFIRMATIONS = {
    "validate": "VALIDATE",
    "set_price": "SET_PRICE",
    "reopen": "REOPEN",
    "schedule_close": "SCHEDULE_CLOSE",
}


def die(message: str) -> None:
    raise SystemExit(message)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True)
    parser.add_argument("--operation", required=True, choices=sorted(CONFIRMATIONS))
    parser.add_argument("--confirm", required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--ad-id")
    parser.add_argument("--new-price", type=int)
    parser.add_argument("--expected-old-price", type=int)
    parser.add_argument("--effective-at")
    return parser.parse_args()


def validate_feed(text: str) -> dict[str, ET.Element]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        die(f"invalid XML: {exc}")
    if root.tag != "Ads":
        die("root must be Ads")

    ads: dict[str, ET.Element] = {}
    for ad in root.findall("Ad"):
        ad_id = (ad.findtext("Id") or "").strip()
        if not ad_id:
            die("every Ad must have Id")
        if ad_id in ads:
            die(f"duplicate Id: {ad_id}")
        if ad_id not in ALLOWED_IDS:
            die(f"active feed contains non-allow-listed Id: {ad_id}")
        if (ad.findtext("ListingFee") or "").strip() != "Package":
            die(f"{ad_id}: ListingFee must remain Package")
        if (ad.findtext("AdStatus") or "Free").strip() != "Free":
            die(f"{ad_id}: paid AdStatus is forbidden in this workflow")
        if (ad.findtext("DeliverySubsidy") or "").strip() != "0":
            die(f"{ad_id}: DeliverySubsidy must remain 0")
        try:
            price = int((ad.findtext("Price") or "").strip())
        except ValueError:
            die(f"{ad_id}: Price must be an integer")
        if price <= 0 or price > 10_000_000:
            die(f"{ad_id}: Price is outside safe range")
        ads[ad_id] = ad

    if set(ads) != ALLOWED_IDS:
        die(f"active feed must contain exactly five base Ads; missing={sorted(ALLOWED_IDS - set(ads))}")
    return ads


def find_ad_block(text: str, ad_id: str) -> tuple[int, int, str]:
    blocks = list(re.finditer(r"(?ms)^\s*<Ad>.*?^\s*</Ad>", text))
    matched = [
        block
        for block in blocks
        if re.findall(r"<Id>\s*([^<]+?)\s*</Id>", block.group(0)) == [ad_id]
    ]
    if len(matched) != 1:
        die(f"expected exactly one Ad block for {ad_id}, got {len(matched)}")
    block = matched[0]
    return block.start(), block.end(), block.group(0)


def replace_single_tag(block: str, tag: str, value: str) -> str:
    pattern = re.compile(rf"(?ms)(<{tag}>).*?(</{tag}>)")
    if len(pattern.findall(block)) != 1:
        die(f"expected exactly one {tag} in selected Ad")
    return pattern.sub(rf"\g<1>{value}\g<2>", block, count=1)


def insert_or_replace_date(block: str, tag: str, value: str) -> str:
    pattern = re.compile(rf"(?ms)^\s*<{tag}>.*?</{tag}>\s*$")
    if pattern.search(block):
        return pattern.sub(f"    <{tag}>{value}</{tag}>", block, count=1)
    marker = re.search(r"(?m)^(\s*)<ListingFee>", block)
    if not marker:
        die("ListingFee marker not found")
    indent = marker.group(1)
    return block[: marker.start()] + f"{indent}<{tag}>{value}</{tag}>\n" + block[marker.start() :]


def remove_date(block: str, tag: str) -> str:
    return re.sub(rf"(?m)^\s*<{tag}>.*?</{tag}>\s*\n?", "", block)


def main() -> None:
    args = parse_args()
    if args.confirm != CONFIRMATIONS[args.operation]:
        die("confirmation token does not match operation")

    path = Path(args.file)
    before = path.read_bytes()
    actual_sha = digest(before)
    if actual_sha != args.expected_sha256:
        die(f"feed SHA256 changed: expected {args.expected_sha256}, got {actual_sha}")

    text = before.decode("utf-8")
    ads = validate_feed(text)
    if args.operation == "validate":
        print(f"changed=false ads={len(ads)} sha256={actual_sha}")
        return

    if not args.ad_id or args.ad_id not in ALLOWED_IDS:
        die("ad-id must be one of the five base listing IDs")
    start, end, block = find_ad_block(text, args.ad_id)

    if args.operation == "set_price":
        if args.new_price is None or args.expected_old_price is None:
            die("set_price requires new-price and expected-old-price")
        current = int((ads[args.ad_id].findtext("Price") or "").strip())
        if current != args.expected_old_price:
            die(f"old price mismatch: expected {args.expected_old_price}, got {current}")
        if args.new_price <= 0 or args.new_price > 10_000_000:
            die("new price is outside safe range")
        block = replace_single_tag(block, "Price", str(args.new_price))
    elif args.operation == "reopen":
        block = remove_date(remove_date(block, "DateBegin"), "DateEnd")
    elif args.operation == "schedule_close":
        if not args.effective_at:
            die("schedule_close requires effective-at")
        try:
            effective = dt.datetime.fromisoformat(args.effective_at)
        except ValueError:
            die("effective-at must be ISO 8601")
        if effective.tzinfo is None:
            die("effective-at must include an explicit timezone")
        if effective.astimezone(dt.timezone.utc) < dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2):
            die("effective-at must be at least two hours in the future")
        block = insert_or_replace_date(block, "DateEnd", effective.isoformat(timespec="seconds"))

    after_text = text[:start] + block + text[end:]
    validate_feed(after_text)
    after = after_text.encode("utf-8")
    if after == before:
        print(f"changed=false ads={len(ads)} sha256={actual_sha}")
        return
    path.write_bytes(after)
    print(f"changed=true ads={len(ads)} sha256={digest(after)} operation={args.operation} ad_id={args.ad_id}")


if __name__ == "__main__":
    main()
