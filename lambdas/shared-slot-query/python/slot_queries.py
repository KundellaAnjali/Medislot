from boto3.dynamodb.conditions import Key


def query_slots_in_range(slots_table, doctor_id, start_time, end_time):
    query = {
        "KeyConditionExpression": Key("doctor_id").eq(doctor_id)
        & Key("slot_start_time").between(start_time, end_time),
    }
    slots = []
    while True:
        result = slots_table.query(**query)
        slots.extend(result.get("Items", []))
        last_key = result.get("LastEvaluatedKey")
        if not last_key:
            return slots
        query["ExclusiveStartKey"] = last_key
