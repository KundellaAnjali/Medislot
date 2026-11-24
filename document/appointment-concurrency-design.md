# Appointment Concurrency and Cancellation Design

## Booking strategy

The Slots DynamoDB item is keyed by `doctor_id` and `slot_start_time`. A booking reads the current `booked_count` and `version`, then performs one conditional `UpdateItem` that increments both values only when:

- The slot exists and is active.
- The stored `booked_count` and `version` still match the values that were read.
- `booked_count < capacity`.
- The slot start is still in the future.

DynamoDB evaluates the condition and update atomically for that item. Of 20 requests observing an empty slot with capacity three, only a request with the current version can update each of the first three seats. Each successful update increments the version, making competing updates based on an older version fail. Requests reload the item and retry conditional conflicts with bounded backoff; once the count reaches capacity, the condition rejects all remaining requests. No request can move the count past capacity. MySQL remains the appointment system of record, and a failed MySQL transaction triggers a compensating seat release.

The former local concurrent test sent 20 requests through the booking Lambda using a thread-safe conditional DynamoDB test double and asserted exactly 3 successes, 17 HTTP 409 responses, `booked_count == 3`, a version increase of exactly 3, and emission of `BookingConflictRetries`. Appointment test files were removed by request; rerun or restore those tests before relying on automated coverage.

Idempotency keys are scoped to the authenticated user and bound to a hash of the booking request. The response and pending event state are cached in the existing DynamoDB idempotency table; no RDS idempotency or outbox table is used. Replays with a cached response do not reserve another seat. Since MySQL and DynamoDB do not share a transaction, strict exactly-once behavior cannot be guaranteed if the SQL commit succeeds but writing the DynamoDB cache fails. A confirmed rollback compensates the DynamoDB reservation; if the database commit outcome cannot be established, the seat is retained rather than risking a double release.

## Cancellation and seat release

Cancellation and refund rows are committed in one MySQL transaction using the existing `appointments` and `refunds` tables. Patient cancellation affects only the authenticated patient's appointment. Doctor cancellation affects every active appointment assigned to the cancelled slot and produces a full refund tier for each. Booking and doctor cancellation both lock the doctor row in MySQL: cancellation disables the slot before it reads all active appointments, and new bookings for that doctor wait until cancellation commits, then observe the inactive slot. If the SQL cancellation fails, the slot remains disabled (fail-closed) and the doctor can retry cancellation; it is never reopened automatically where that could admit a booking during an uncertain transaction outcome.

After the MySQL commit, the cancellation Lambda frees each seat using one DynamoDB `TransactWriteItems` request: it conditionally inserts a unique release marker and conditionally decrements `booked_count` while incrementing `version`. The marker and counter change either both commit or neither commits. If the cancellation request retries after DynamoDB committed, the marker proves that the counter must not be decremented again. The Lambda then sends `AppointmentCancelled` and `SlotSeatFreed` directly to EventBridge. Because no durable RDS outbox is used, a failure between the SQL commit and follow-up processing requires the cancellation request to be retried.

EventBridge delivery is at-least-once; consumers should deduplicate each event by `appointment_id` and event type.

## Refund boundaries

Refund settings are read from `/medislot/{environment}/FULL_REFUND_HOURS`, `/PARTIAL_REFUND_HOURS`, and `/PARTIAL_REFUND_PERCENT`. At exactly 24 hours, the partial tier applies; full refund requires strictly more than 24 hours. At exactly 12 hours, the partial tier still applies; no refund applies strictly below 12 hours. Doctor-initiated slot cancellations override the time windows and use the full tier.

The ledger records calculated refund liability from successful payment amounts. There is no payment-provider integration in the configured services, so this implementation does not initiate a card/bank transfer; connect the ledger transaction to a payment processor before treating the amount as externally refunded.
 
it have some notifications thing but not fully completed
