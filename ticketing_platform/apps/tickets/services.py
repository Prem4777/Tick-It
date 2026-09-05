"""
Tickets services — all inventory-mutating operations live here.

Every function that touches ticket counts or check-in state runs inside a
transaction.atomic() with select_for_update() on the row being mutated.
This is the primary defence against overselling and duplicate check-ins.

SQLite note: SQLite locks the entire DB file on any write transaction, so
select_for_update() is technically a no-op there. The code is still written
correctly so it degrades gracefully if the DB is upgraded to Postgres.
"""

from datetime import timedelta
from io import BytesIO
import secrets

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import F
from django.utils import timezone
import qrcode

from .models import Reservation, Ticket, TicketType
from apps.events.models import Event

# How long a reservation holds inventory before it auto-expires.
RESERVATION_LOCK_MINUTES = 10


# ---------------------------------------------------------------------------
# Reservation (cart locking)
# ---------------------------------------------------------------------------

def reserve_tickets(ticket_type_id, user, quantity):
    """
    Create a time-limited reservation for `quantity` tickets of the given tier.

    Uses select_for_update() on the TicketType row to prevent two concurrent
    requests from both seeing available_count > 0 and both creating reservations
    that together exceed the available stock (classic read-then-write race).

    Raises ValidationError if:
      - quantity < 1
      - the event is not published
      - available stock is less than requested
    """
    if quantity < 1:
        raise ValidationError("Quantity must be at least 1.")

    with transaction.atomic():
        # Lock the row — any concurrent reserve_tickets() for the same tier
        # will block here until this transaction commits.
        ticket_type = TicketType.objects.select_for_update().get(
            pk=ticket_type_id
        )

        if ticket_type.event.status != Event.Status.PUBLISHED:
            raise ValidationError(
                "Tickets are not available for this event yet."
            )

        # Re-read available_count after acquiring the lock to get post-lock state.
        available = ticket_type.available_count()
        if quantity > available:
            raise ValidationError(
                f"Only {available} ticket(s) left for {ticket_type.name}."
            )

        return Reservation.objects.create(
            ticket_type=ticket_type,
            user=user,
            quantity=quantity,
            expires_at=timezone.now() + timedelta(minutes=RESERVATION_LOCK_MINUTES),
            status=Reservation.Status.ACTIVE,
        )


# ---------------------------------------------------------------------------
# Ticket generation helpers
# ---------------------------------------------------------------------------

def generate_unique_code():
    """
    Generate a URL-safe random token that doesn't already exist in the DB.
    secrets.token_urlsafe(24) produces ~32 characters of high-entropy text.
    Collision probability is negligible but we loop to be safe.
    """
    while True:
        code = secrets.token_urlsafe(24)
        if not Ticket.objects.filter(unique_code=code).exists():
            return code


def _create_ticket(ticket_type, user):
    """
    Create a Ticket record and generate its QR code PNG.

    Internal helper called from checkout_cart() and promote_next().
    Not intended to be called directly from views.
    """
    code = generate_unique_code()

    # Save the ticket record first so we have a PK before attaching the image.
    ticket = Ticket(
        ticket_type=ticket_type,
        event=ticket_type.event,
        user=user,
        unique_code=code,
        status=Ticket.Status.ACTIVE,
    )
    ticket.save()

    # Generate a QR PNG in memory and attach it to the ticket's ImageField.
    img = qrcode.make(code)
    buf = BytesIO()
    img.save(buf, format="PNG")
    ticket.qr_image.save(
        f"{code}.png", ContentFile(buf.getvalue()), save=True
    )
    return ticket


# ---------------------------------------------------------------------------
# Checkout
# ---------------------------------------------------------------------------

def checkout_cart(user):
    """
    Convert all active, non-expired reservations for a user into real Tickets.

    Locks all relevant TicketType rows before mutating quantity_sold to prevent
    concurrent checkouts from double-counting the same reservation.

    Raises ValidationError if:
      - the cart is empty or all reservations have expired
      - a reservation expires in the moment between cart view and checkout submit
      - not enough tickets remain (should be rare due to reservation locking)

    Returns a list of created Ticket objects.
    """
    now = timezone.now()

    # Snapshot the active reservations before opening the transaction.
    reservations = list(
        Reservation.objects.filter(
            user=user,
            status=Reservation.Status.ACTIVE,
            expires_at__gt=now,
        ).select_related("ticket_type")
    )

    if not reservations:
        raise ValidationError("Your cart is empty or all holds have expired.")

    # Lock all affected TicketType rows in a deterministic order to avoid
    # deadlocks when two users check out overlapping tiers simultaneously.
    type_pks = sorted({r.ticket_type_id for r in reservations})

    with transaction.atomic():
        locked_types = {
            tt.pk: tt
            for tt in TicketType.objects.select_for_update().filter(
                pk__in=type_pks
            )
        }

        created_tickets = []
        for r in reservations:
            tt = locked_types[r.ticket_type_id]

            # Re-read the reservation after acquiring the lock to catch any
            # expiry that happened between the snapshot and now.
            r.refresh_from_db()
            if r.status != Reservation.Status.ACTIVE or r.expires_at <= now:
                raise ValidationError(
                    "A reservation expired during checkout. Please try again."
                )

            # Final stock check inside the lock.
            if tt.quantity_sold + r.quantity > tt.quantity_total:
                raise ValidationError(
                    f"Not enough tickets left for {tt.name}."
                )

            # Mark reservation as converted so it doesn't show in the cart
            # and can't be used again.
            r.status = Reservation.Status.CONVERTED
            r.save()

            # Increment sold count.
            tt.quantity_sold += r.quantity
            tt.save()

            # Create one Ticket record per unit purchased.
            for _ in range(r.quantity):
                ticket = _create_ticket(tt, user)
                created_tickets.append(ticket)

        return created_tickets


# ---------------------------------------------------------------------------
# Check-in (V4)
# ---------------------------------------------------------------------------

def checkin_ticket(qr_code, event_id, staff_user):
    """
    Check in a single ticket by scanning its QR code.

    Uses a conditional UPDATE (WHERE checked_in_at IS NULL) as the
    concurrency lock — if two scanners scan the same code simultaneously,
    only one UPDATE will affect a row. The loser gets rows_updated=0 and
    returns 'already_checked_in'.

    Args:
        qr_code:    The ticket's unique_code string.
        event_id:   ID of the event — ensures the ticket belongs to this event.
        staff_user: The User performing the check-in (stored for audit trail).

    Returns a dict with key 'status' and one of:
        checked_in        — success, ticket is now marked as checked in.
        already_checked_in — ticket was already scanned.
        invalid_qr        — code not found in the system.
        wrong_event       — code belongs to a different event.
    """
    now = timezone.now()

    with transaction.atomic():
        # Atomic conditional update — only succeeds if not already checked in.
        # This is the primary duplicate-scan prevention mechanism.
        rows_updated = Ticket.objects.filter(
            unique_code=qr_code,
            event_id=event_id,
            checked_in_at__isnull=True,   # Only match un-scanned tickets
        ).update(
            checked_in_at=now,
            checked_in_by=staff_user,
        )

        if rows_updated == 1:
            # Success — fetch the updated ticket to return its ID.
            ticket = Ticket.objects.get(unique_code=qr_code, event_id=event_id)
            return {
                "status": "checked_in",
                "ticket_id": str(ticket.pk),
                "checked_in_at": ticket.checked_in_at.isoformat(),
                "checked_in_by": str(ticket.checked_in_by) if ticket.checked_in_by else None,
            }

        # rows_updated == 0 — diagnose why.
        ticket = Ticket.objects.filter(unique_code=qr_code).first()

        if ticket is None:
            return {
                "status": "invalid_qr",
                "message": "QR code not found or does not belong to this event",
            }

        if ticket.event_id != event_id:
            return {
                "status": "wrong_event",
                "message": "Ticket belongs to another event",
            }

        # Ticket exists for this event but was already checked in.
        return {
            "status": "already_checked_in",
            "ticket_id": str(ticket.pk),
            "message": "Ticket has already been checked in",
        }


def bulk_checkin_tickets(qr_codes, event_id, staff_user):
    """
    Check in multiple tickets in a single request.

    Each code is processed independently — one failure does not roll back
    the others. Duplicate codes within the same batch are detected before
    hitting the database.

    Args:
        qr_codes:   List of unique_code strings (max 100).
        event_id:   All tickets must belong to this event.
        staff_user: The User performing the check-ins.

    Returns a dict with:
        results         — list of per-code result dicts (same shape as checkin_ticket).
        total_processed — number of codes submitted.
    """
    MAX_BATCH = 100

    if not isinstance(qr_codes, list) or len(qr_codes) == 0:
        return {"results": [], "total_processed": 0}

    if len(qr_codes) > MAX_BATCH:
        return {
            "results": [{"qr_code": c, "status": "batch_too_large",
                         "message": f"Batch exceeds maximum of {MAX_BATCH}"}
                        for c in qr_codes],
            "total_processed": len(qr_codes),
        }

    now = timezone.now()

    # Deduplicate within the request — track which codes appear more than once.
    seen = set()
    unique_codes = []
    is_duplicate = {}
    for code in qr_codes:
        if code in seen:
            is_duplicate[code] = True
        else:
            seen.add(code)
            is_duplicate[code] = False
            unique_codes.append(code)

    results = [None] * len(qr_codes)

    with transaction.atomic():
        # Batch-fetch all matching tickets in one query to minimise DB round-trips.
        ticket_map = {
            t.unique_code: t
            for t in Ticket.objects.filter(
                unique_code__in=unique_codes,
                event_id=event_id,
            )
        }

        processed = set()

        for qr_code in unique_codes:
            if qr_code in processed:
                continue
            processed.add(qr_code)

            # Find all positions in the original list for this code.
            positions = [i for i, c in enumerate(qr_codes) if c == qr_code]

            # Mark any duplicate occurrences (beyond the first) immediately.
            for pos in positions[1:]:
                results[pos] = {
                    "qr_code": qr_code,
                    "status": "duplicate_in_request",
                    "message": "Duplicate QR code within same request",
                }

            first_pos = positions[0]

            if qr_code not in ticket_map:
                results[first_pos] = {
                    "qr_code": qr_code,
                    "status": "invalid_qr",
                    "message": "QR code not found or does not belong to this event",
                }
                continue

            ticket = ticket_map[qr_code]

            if ticket.status != Ticket.Status.ACTIVE:
                results[first_pos] = {
                    "qr_code": qr_code,
                    "status": "ticket_not_eligible",
                    "message": "Ticket is not available for check-in",
                }
                continue

            # Conditional update — same concurrency pattern as checkin_ticket().
            rows_updated = Ticket.objects.filter(
                pk=ticket.pk,
                checked_in_at__isnull=True,
            ).update(
                checked_in_at=now,
                checked_in_by=staff_user,
            )

            if rows_updated == 1:
                results[first_pos] = {
                    "qr_code": qr_code,
                    "status": "checked_in",
                    "ticket_id": str(ticket.pk),
                    "message": "Ticket checked in successfully",
                }
            else:
                # Another request checked this in concurrently.
                results[first_pos] = {
                    "qr_code": qr_code,
                    "status": "already_checked_in",
                    "ticket_id": str(ticket.pk),
                    "message": "Ticket has already been checked in",
                }

    # Safety fill for any positions that weren't reached (should not happen).
    for i, r in enumerate(results):
        if r is None:
            results[i] = {
                "qr_code": qr_codes[i],
                "status": "processing_error",
                "message": "Failed to process this QR code",
            }

    return {"results": results, "total_processed": len(qr_codes)}
