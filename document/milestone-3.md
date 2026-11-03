# Milestone 3: Doctor Domain

The doctor domain is deployed as `dev-medislot-doctor-stack`. It attaches `/doctors/me` and its child paths to the REST API created by `dev-medislot-auth-stack`; it does not create another API. Protected routes use the existing JWT authorizer and accept only the `DOCTOR` role. Handler identity comes from `requestContext.authorizer.userId`, never from request data.

## Routes

| Method | Path | Purpose |
| --- | --- | --- |
| GET, PUT | `/doctors/me` | Read or update the professional profile, hospital details, and specializations |
| POST | `/doctors/me/photo` | Create a short-lived S3 upload URL for JPG, PNG, or WebP |
| GET, POST | `/doctors/me/consultation-types` | List or create a consultation type |
| PUT, DELETE | `/doctors/me/consultation-types/{id}` | Update or remove a consultation type |
| GET, PUT | `/doctors/me/availability` | Read or replace recurring weekly patterns |
| GET, POST | `/doctors/me/blackouts` | List blackouts or add one date/date range |
| DELETE | `/doctors/me/blackouts/{id}` | Remove a blackout |
| GET | `/doctors/me/schedule?date=YYYY-MM-DD` | Read a UTC-date schedule with confirmed patient bookings |

Consultation type durations and capacities follow the frozen rules. A blackout request accepts `{"date":"YYYY-MM-DD"}` or inclusive `start_date` and `end_date`. Availability PUT replaces the doctor's complete weekly pattern set.

## Slot Materialization

`SlotMaterializerFunction` runs daily at 02:00 UTC through EventBridge Scheduler. Availability and blackout dates use UTC throughout; there is no per-doctor timezone conversion. The rolling window is `/medislot/{environment}/booking-window-days` in SSM Parameter Store, initially 30 days. Slot start and end are stored as UTC timestamps. IDs are deterministic and DynamoDB `attribute_not_exists(slot_id)` makes reruns safe.

The slots table's `DoctorStartIndex` supports the doctor day view. Appointment `slot_id` is migrated to `BIGINT` so it can hold the deterministic DynamoDB slot identifier. The schema initializer removes the old doctor timezone column and checks the slot ID type idempotently.

## Deployment

The workflow deploys network, data, IAM, and auth first, then uploads `web/doctor-portal` to the assets bucket exported by the data stack and deploys the doctor stack. It creates a new deployment for the existing API stage after adding the doctor resources. IAM roles and the EC2 instance profile are defined in the IAM stack; the HTTP security group is defined in the network stack. The doctor portal EC2 instance is in the existing public subnet; RDS remains private. Set `PortalAccessCidr` in the network parameters to the intended source range.

The portal opens on a doctor registration-first welcome page, then supports sign-in, profile and consultation editing, availability editing, UTC day schedule viewing, and photo upload to the existing assets bucket. Access and refresh tokens are held in Flask's server-side filesystem session. The portal uses the platform API and has no database credentials.