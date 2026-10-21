"""Business metrics. Labels are low-cardinality only: never PNRs or names."""

from prometheus_client import Counter, Histogram

BOOKINGS = Counter(
    "railbook_bookings_total",
    "Booking attempts by outcome",
    ["status"],  # confirmed | sold_out | payment_failed | not_found
)
CANCELLATIONS = Counter("railbook_cancellations_total", "Cancelled bookings")
BOOKING_DURATION = Histogram(
    "railbook_booking_duration_seconds",
    "Time to process a booking request",
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5),
)
