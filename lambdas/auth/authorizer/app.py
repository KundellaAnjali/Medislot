import json
import os
import logging
from datetime import datetime, timezone

import boto3
import jwt
from boto3.dynamodb.conditions import Key
from jwt import ExpiredSignatureError
from jwt import InvalidTokenError


logger = logging.getLogger()
logger.setLevel(logging.INFO)

ssm = boto3.client("ssm")
dynamodb = boto3.resource("dynamodb")

ENVIRONMENT = os.environ["ENVIRONMENT_NAME"]

parameter_cache = {}


def get_parameter(name, decrypt=False):

    if name in parameter_cache:
        return parameter_cache[name]

    response = ssm.get_parameter(
        Name=name,
        WithDecryption=decrypt
    )

    value = response["Parameter"]["Value"]

    parameter_cache[name] = value

    return value


def generate_policy(
    principal_id,
    effect,
    resource,
    context=None
):

    policy = {
        "principalId": principal_id,
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Action": "execute-api:Invoke",
                    "Effect": effect,
                    "Resource": resource
                }
            ]
        }
    }

    if context:
        policy["context"] = context

    return policy


def lambda_handler(event, context):

    try:

        token = event.get("authorizationToken")

        if not token:
            raise Exception("Missing authorization token")

        if token.startswith("Bearer "):
            token = token.removeprefix("Bearer ")

        jwt_secret = get_parameter(
            f"/medislot/{ENVIRONMENT}/jwt-secret",
            decrypt=True
        )

        decoded_token = jwt.decode(
            token,
            jwt_secret,
            algorithms=["HS256"]
        )

        token_id = decoded_token.get("jti")
        if not token_id:
            raise InvalidTokenError("Missing token ID")

        sessions = dynamodb.Table(f"{ENVIRONMENT}-user-sessions")
        session_response = sessions.get_item(
            Key={"session_id": token_id},
            ConsistentRead=True,
        )
        active_session = session_response.get("Item")
        if (
            not active_session
            or active_session.get("access_token_jti") != token_id
            or int(active_session["expires_at"]) <= int(
            datetime.now(timezone.utc).timestamp()
            )
        ):
            raise InvalidTokenError("Access token session is no longer active")

        return generate_policy(
            decoded_token["sub"],
            "Allow",
            event["methodArn"],
            {
                "userId": str(decoded_token["sub"]),
                "email": str(decoded_token["email"]),
                "role": str(decoded_token["role"])
            }
        )

    except ExpiredSignatureError:

        logger.error(
            json.dumps(
                {
                    "event": "TokenExpired"
                }
            )
        )

        return generate_policy(
            "anonymous",
            "Deny",
            event["methodArn"]
        )

    except InvalidTokenError:

        logger.error(
            json.dumps(
                {
                    "event": "InvalidToken"
                }
            )
        )

        return generate_policy(
            "anonymous",
            "Deny",
            event["methodArn"]
        )

    except Exception as exception:

        logger.error(
            json.dumps(
                {
                    "event": "AuthorizationFailed",
                    "error": str(exception)
                }
            )
        )

        return generate_policy(
            "anonymous",
            "Deny",
            event["methodArn"]
        )