import logging

try:
    from .waitlist_workflow import expire_due_offers, promote_slot
except ImportError:
    from waitlist_workflow import expire_due_offers, promote_slot


logger = logging.getLogger()
logger.setLevel(logging.INFO)


def promotion_lambda_handler(event, context):
    detail = event.get("detail", event)
    if detail.get("status") != "SEAT_FREED":
        logger.info("Ignoring non-seat-freed event: detail_type=%s", event.get("detail-type"))
        return {"success": True, "promoted": False}
    try:
        return promote_slot(detail)
    except Exception:
        logger.exception("Could not promote a waitlisted patient")
        raise


def expiry_lambda_handler(event, context):
    try:
        expired_count = expire_due_offers()
    except Exception:
        logger.exception("Waitlist offer expiry sweep failed")
        raise
    return {"success": True, "expired_count": expired_count}
