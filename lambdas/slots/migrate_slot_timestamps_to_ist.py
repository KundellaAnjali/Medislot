import argparse
import copy
import json
import re
from datetime import datetime, timedelta, timezone

import boto3
from boto3.dynamodb.types import TypeSerializer


INDIA_TIMEZONE = timezone(timedelta(hours=5, minutes=30))
ACTIVE_OFFER_LOCK_PATTERN = re.compile(r"(waitlist-active-offer:\d+:)(.+)")
SERIALIZER = TypeSerializer()
KEY_FIELDS = {
    "slots": ("doctor_id", "slot_start_time"),
    "waitlists": ("slot_id", "joined_at"),
    "offers": ("offer_id",),
    "idempotency": ("idempotency_key",),
}


def india_timestamp(value):
    if not isinstance(value, str):
        return value
    parsed = datetime.fromisoformat(value.replace("Z", "+05:30"))
    parsed = parsed.replace(tzinfo=INDIA_TIMEZONE)
    precision = "microseconds" if parsed.microsecond else "seconds"
    return parsed.isoformat(timespec=precision)


def scan_all(table):
    items = []
    parameters = {}
    while True:
        page = table.scan(**parameters)
        items.extend(page.get("Items", []))
        last_key = page.get("LastEvaluatedKey")
        if not last_key:
            return items
        parameters["ExclusiveStartKey"] = last_key


def build_plans(tables):
    plans = []
    transformed_slot_keys = set()
    transformed_lock_keys = set()
    slots = scan_all(tables["slots"])
    existing_slot_keys = {
        (int(item["doctor_id"]), item["slot_start_time"])
        for item in slots
    }

    for item in slots:
        old_start = item["slot_start_time"]
        new_start = india_timestamp(old_start)
        new_item = copy.deepcopy(item)
        new_item["slot_start_time"] = new_start
        if "slot_end_time" in new_item:
            new_item["slot_end_time"] = india_timestamp(new_item["slot_end_time"])
        if new_item == item:
            continue
        key = (int(new_item["doctor_id"]), new_start)
        old_key = (int(item["doctor_id"]), old_start)
        if new_start != old_start:
            if key in transformed_slot_keys or (
                key in existing_slot_keys and key != old_key
            ):
                raise RuntimeError(f"Slot key collision after IST conversion: {key}")
            transformed_slot_keys.add(key)
        plans.append(("slots", item, new_item))

    for table_name in ("waitlists", "offers"):
        for item in scan_all(tables[table_name]):
            old_start = item.get("slot_start_time")
            new_start = india_timestamp(old_start)
            if old_start is None or new_start == old_start:
                continue
            new_item = copy.deepcopy(item)
            new_item["slot_start_time"] = new_start
            plans.append((table_name, item, new_item))

    idempotency_items = scan_all(tables["idempotency"])
    existing_idempotency_keys = {
        item["idempotency_key"] for item in idempotency_items
    }
    for item in idempotency_items:
        new_item = copy.deepcopy(item)
        old_key = item["idempotency_key"]
        match = ACTIVE_OFFER_LOCK_PATTERN.fullmatch(old_key)
        if match:
            new_key = match.group(1) + india_timestamp(match.group(2))
            new_item["idempotency_key"] = new_key
            if new_key in transformed_lock_keys or (
                new_key in existing_idempotency_keys and new_key != old_key
            ):
                raise RuntimeError(f"Active offer lock collision: {new_key}")
            transformed_lock_keys.add(new_key)

        payload = new_item.get("response_payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = None
        if isinstance(payload, dict) and payload.get("slot_start_time"):
            converted = india_timestamp(payload["slot_start_time"])
            if converted != payload["slot_start_time"]:
                payload["slot_start_time"] = converted
                new_item["response_payload"] = json.dumps(
                    payload, separators=(",", ":")
                )
        if new_item != item:
            plans.append(("idempotency", item, new_item))

    return plans


def backup_plans(path, plans):
    with open(path, "x", encoding="utf-8") as backup:
        for table_name, old_item, _ in plans:
            serialized = {
                name: SERIALIZER.serialize(value)
                for name, value in old_item.items()
            }
            backup.write(
                json.dumps(
                    {"table": table_name, "item": serialized},
                    separators=(",", ":"),
                )
                + "\n"
            )


def apply_plan(client, table_names, tables, table_name, old_item, new_item):
    key_fields = KEY_FIELDS[table_name]
    old_key = {field: old_item[field] for field in key_fields}
    new_key = {field: new_item[field] for field in key_fields}
    if old_key != new_key:
        key_attribute = key_fields[0]
        client.transact_write_items(
            TransactItems=[
                {
                    "Put": {
                        "TableName": table_names[table_name],
                        "Item": {
                            name: SERIALIZER.serialize(value)
                            for name, value in new_item.items()
                        },
                        "ConditionExpression": "attribute_not_exists(#key)",
                        "ExpressionAttributeNames": {"#key": key_attribute},
                    }
                },
                {
                    "Delete": {
                        "TableName": table_names[table_name],
                        "Key": {
                            name: SERIALIZER.serialize(value)
                            for name, value in old_key.items()
                        },
                        "ConditionExpression": "attribute_exists(#key)",
                        "ExpressionAttributeNames": {"#key": key_fields[0]},
                    }
                },
            ]
        )
        return

    changed = {
        name: value
        for name, value in new_item.items()
        if name not in key_fields and old_item.get(name) != value
    }
    if not changed:
        return
    names = {f"#n{index}": name for index, name in enumerate(changed)}
    values = {
        f":v{index}": SERIALIZER.serialize(value)
        for index, value in enumerate(changed.values())
    }
    expression = "SET " + ", ".join(
        f"#n{index} = :v{index}" for index in range(len(changed))
    )
    tables[table_name].update_item(
        Key=old_key,
        UpdateExpression=expression,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Re-key legacy UTC-formatted appointment slots as IST while "
            "preserving the displayed clock time."
        ),
        epilog=(
            "Run a dry run first. Stop appointment booking, slot materialization, "
            "rescheduling, and waitlist writers before applying. The backup file "
            "contains original DynamoDB records and must be protected. MySQL "
            "DATETIME appointment and blackout values are intentionally unchanged."
        ),
    )
    parser.add_argument("--slots-table", required=True)
    parser.add_argument("--waitlists-table", required=True)
    parser.add_argument("--offers-table", required=True)
    parser.add_argument("--idempotency-table", required=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply changes; otherwise only report the number of records.",
    )
    parser.add_argument(
        "--confirm-no-writers",
        action="store_true",
        help="Confirm appointment and waitlist writers are stopped.",
    )
    parser.add_argument(
        "--backup-file",
        help="New JSONL file for original DynamoDB records; required with --apply.",
    )
    arguments = parser.parse_args()

    if arguments.apply and not arguments.confirm_no_writers:
        parser.error("--apply requires --confirm-no-writers.")
    if arguments.apply and not arguments.backup_file:
        parser.error("--apply requires --backup-file.")

    table_names = {
        "slots": arguments.slots_table,
        "waitlists": arguments.waitlists_table,
        "offers": arguments.offers_table,
        "idempotency": arguments.idempotency_table,
    }
    dynamodb = boto3.resource("dynamodb")
    tables = {
        key: dynamodb.Table(name)
        for key, name in table_names.items()
    }
    plans = build_plans(tables)
    print(f"Records requiring IST conversion: {len(plans)}")
    if not arguments.apply or not plans:
        return

    backup_plans(arguments.backup_file, plans)
    client = boto3.client("dynamodb")
    for table_name, old_item, new_item in plans:
        apply_plan(client, table_names, tables, table_name, old_item, new_item)
    print(f"IST conversion complete. Original records saved to {arguments.backup_file}")


if __name__ == "__main__":
    main()
