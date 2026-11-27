import logging

try:
    from .appointment_repository import AppointmentRepository
except ImportError:
    from appointment_repository import AppointmentRepository


logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event, context):
    try:
        completed_count = AppointmentRepository().complete_elapsed_appointments()
    except Exception:
        logger.exception("Scheduled appointment completion failed")
        raise
    return {"success": True, "completed_count": completed_count}
