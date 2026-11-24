import logging

import pymysql

from profile import get_connection, response


logger = logging.getLogger()
logger.setLevel(logging.INFO)

SEARCH_FIELDS = ("name", "hospital", "specialization", "city")
DEFAULT_PAGE = 1
DEFAULT_LIMIT = 10
MAX_LIMIT = 100
MAX_FILTER_LENGTH = 100


class SearchValidationError(Exception):
    pass


def parse_search(event):
    query = event.get("queryStringParameters") or {}
    filters = {}
    for field in SEARCH_FIELDS:
        value = query.get(field, "")
        if not isinstance(value, str):
            raise SearchValidationError(f"{field} must be text.")
        value = value.strip()
        if len(value) > MAX_FILTER_LENGTH:
            raise SearchValidationError(
                f"{field} must be {MAX_FILTER_LENGTH} characters or fewer."
            )
        filters[field] = value

    try:
        page = int(query.get("page", DEFAULT_PAGE))
        limit = int(query.get("limit", DEFAULT_LIMIT))
    except (TypeError, ValueError) as error:
        raise SearchValidationError("page and limit must be whole numbers.") from error

    if page < 1:
        raise SearchValidationError("page must be at least 1.")
    if limit < 1 or limit > MAX_LIMIT:
        raise SearchValidationError(f"limit must be between 1 and {MAX_LIMIT}.")
    if (page - 1) * limit > 2**63 - 1:
        raise SearchValidationError("Requested page is too large.")
    return filters, page, limit


def contains_pattern(value):
    escaped = value.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    return f"%{escaped}%"


def build_filters(filters):
    clauses = []
    values = []

    if filters["name"]:
        pattern = contains_pattern(filters["name"])
        clauses.append(
            "(d.first_name LIKE %s ESCAPE '!' "
            "OR d.last_name LIKE %s ESCAPE '!' "
            "OR CONCAT_WS(' ', d.first_name, d.last_name) LIKE %s ESCAPE '!')"
        )
        values.extend((pattern, pattern, pattern))

    if filters["hospital"]:
        clauses.append("h.hospital_name LIKE %s ESCAPE '!'")
        values.append(contains_pattern(filters["hospital"]))

    if filters["city"]:
        clauses.append("h.city LIKE %s ESCAPE '!'")
        values.append(contains_pattern(filters["city"]))

    if filters["specialization"]:
        clauses.append(
            """
            EXISTS (
                SELECT 1
                FROM doctor_specializations ds_filter
                JOIN specializations s_filter
                  ON s_filter.specialization_id = ds_filter.specialization_id
                WHERE ds_filter.doctor_id = d.doctor_id
                  AND s_filter.specialization_name LIKE %s ESCAPE '!'
            )
            """
        )
        values.append(contains_pattern(filters["specialization"]))

    return clauses, values


def search_doctors(connection, filters, page, limit):
    clauses, filter_values = build_filters(filters)
    where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT COUNT(DISTINCT d.doctor_id) AS total
            FROM doctors d
            LEFT JOIN hospitals h ON h.hospital_id = d.hospital_id
            {where_clause}
            """,
            tuple(filter_values),
        )
        total = cursor.fetchone()["total"]

        cursor.execute(
            f"""
            SELECT
                d.doctor_id,
                TRIM(CONCAT_WS(' ', d.first_name, d.last_name)) AS name,
                h.hospital_name AS hospital,
                GROUP_CONCAT(
                    DISTINCT s.specialization_name
                    ORDER BY s.specialization_name
                    SEPARATOR ', '
                ) AS specialization,
                h.city,
                d.is_verified AS verification_status
            FROM doctors d
            LEFT JOIN hospitals h ON h.hospital_id = d.hospital_id
            LEFT JOIN doctor_specializations ds
              ON ds.doctor_id = d.doctor_id
            LEFT JOIN specializations s
              ON s.specialization_id = ds.specialization_id
            {where_clause}
            GROUP BY
                d.doctor_id,
                d.first_name,
                d.last_name,
                h.hospital_name,
                h.city,
                d.is_verified
            ORDER BY d.doctor_id
            LIMIT %s OFFSET %s
            """,
            tuple(filter_values) + (limit, (page - 1) * limit),
        )
        doctors = cursor.fetchall()

    total_pages = (total + limit - 1) // limit
    return {
        "doctors": doctors,
        "pagination": {
            "page": page,
            "limit": limit,
            "total": total,
            "total_pages": total_pages,
        },
    }


def lambda_handler(event, context):
    authorizer = event.get("requestContext", {}).get("authorizer", {})
    if not authorizer.get("userId"):
        return response(401, {"message": "Authentication is required."})
    if authorizer.get("role") != "PATIENT":
        return response(
            403,
            {"message": "You are not allowed to search the patient directory."},
        )

    method = event.get("httpMethod", "").upper()
    resource = event.get("resource", "")
    path = event.get("path", "").rstrip("/")
    if method != "GET" or not (
        resource == "/doctors/search" or path.endswith("/doctors/search")
    ):
        return response(404, {"message": "Route not found."})

    try:
        filters, page, limit = parse_search(event)
    except SearchValidationError as error:
        return response(400, {"message": str(error)})

    try:
        connection = get_connection()
        try:
            results = search_doctors(connection, filters, page, limit)
        finally:
            connection.close()
        logger.info(
            "Doctor search completed: user_id=%s page=%s limit=%s total=%s request_id=%s",
            authorizer["userId"],
            page,
            limit,
            results["pagination"]["total"],
            getattr(context, "aws_request_id", "unknown"),
        )
        return response(200, results)
    except pymysql.MySQLError:
        logger.exception(
            "Doctor search database request failed: request_id=%s",
            getattr(context, "aws_request_id", "unknown"),
        )
        return response(500, {"message": "Could not search doctors right now."})
