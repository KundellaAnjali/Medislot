import json


def lambda_handler(event, context):

    authorizer = event["requestContext"]["authorizer"]

    if authorizer["role"] != "DOCTOR":
        return {
            "statusCode": 403,
            "body": json.dumps(
                {
                    "message": "Doctor access required"
                }
            )
        }

    return {
        "statusCode": 200,
        "body": json.dumps(
            {
                "message": "Doctor endpoint accessible",
                "userId": authorizer["userId"],
                "role": authorizer["role"]
            }
        )
    }