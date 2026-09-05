# Design Document — Event Ticketing & QR Validation Platform

This document describes the **technical design** of the system: architecture,
data model, concurrency mechanics, request flows, and security boundaries.
For the feature roadmap and version scope, see `PROJECT_PLAN.md` — this file
goes one level deeper into *how* things work.

Status reflected here: **V1 and V2 implemented**, V3–V5 designed but not yet
built. Gaps between design and current implementation are called out
explicitly in Section 9

---

## 1. Architecture Overview

```
Browser
  │
  ▼
Django (WSGI) ── URLconf (config/urls.py)
  │
  ├── apps/accounts   → auth, roles, Profile
  ├── apps/events      → Venue, Event, capacity rules
  ├── apps/tickets      → TicketType, Reservation, cart-lock service
  ├── apps/checkin       → (V4, not built) secret-link scan endpoint
  └── apps/waitlist       → (V5, not built) CSV import, waitlist promotion
  │
  ▼
Jinja2 templates (templates/jinja2/<app>/*.html)
  │
  ▼
SQLite (db.sqlite3)
```

**Two template engines are registered** (`config/settings.py` `TEMPLATES`):
Jinja2 is the primary engine for all app-facing pages; the stock Django
engine is retained solely because `django.contrib.admin` requires it. No app
page uses the Django engine.

**No DRF.** All JSON endpoints (currently none until V4) are hand-written
views returning `JsonResponse`.

---

## 2. Roles & Access Model

| Role | Backing | Enforcement mechanism |
|---|---|---|
| Admin | Django `is_superuser` | Bypasses all app-level checks via `/admin` — not modeled in domain logic |
| Organizer | `Profile.role == 'organizer'` | `organizer_required` decorator (`apps/accounts/decorators.py`) + queryset scoping (`owner=request.user` / `organizer=request.user`) on every view |
| Attendee | `Profile.role == 'attendee'` (default) | Standard `@login_required` where needed; otherwise public read access to published events |
| Check-in scanner | **No account** (V4) | Per-event `scan_token` in the URL is the auth — see Section 6 |

**`Profile`** is a `OneToOneField` to `auth.User`, auto-created via a
`post_save` signal (`apps/accounts/signals.py`) so `request.user.profile`
can be assumed to exist anywhere downstream without a defensive `try/except`.

**Ownership scoping is enforced at the queryset level, not just the UI.**
Every organizer-facing view filters by ownership before rendering or
mutating — e.g.:
```python
Venue.objects.filter(owner=request.user)
Event.objects.filter(organizer=request.user)
```
Dropdowns (e.g. venue selection on the Event form) are scoped identically, so
a crafted POST referencing another organizer's object ID is rejected by the
queryset, not just hidden from the form.

---

## 3. Data Model

### 3.1 Entity-Relationship Summary

```
User (Django built-in)
 │
 ├─1:1─ Profile               role: organizer | attendee
 │
 ├─1:N─ Venue (owner)         name, address, max_capacity
 │        │
 │        └─1:N─ Event (PROTECT on delete)
 │                 │  organizer, name, description, date,
 │                 │  allocated_capacity, status
 │                 │  [planned: public_slug, banner_image,
 │                 │   accent_color, scan_token]
 │                 │
 │                 └─1:N─ TicketType
 │                          │  name, price, quantity_total, quantity_sold
 │                          │
 │                          └─1:N─ Reservation
 │                                   user, quantity, expires_at, status
 │                                   [V3: → converts to Ticket]
 │                                   [V5: WaitlistEntry parallels this]
 │
 └─1:N─ Reservation.user, Event.organizer, Venue.owner (all FK → User)
```

### 3.2 Field-Level Schema (as implemented, V1+V2)

**`Profile`** (`apps/accounts/models.py`)
| Field | Type | Notes |
|---|---|---|
| `user` | `OneToOneField(User)` | `on_delete=CASCADE`, `related_name='profile'` |
| `role` | `CharField(max_length=20)` | `TextChoices`: `organizer`, `attendee`; default `attendee` |

Method: `is_organizer` → `role == Role.ORGANIZER`.

**`Venue`** (`apps/events/models.py`)
| Field | Type | Notes |
|---|---|---|
| `name` | `CharField(200)` | |
| `address` | `TextField` | blank allowed |
| `max_capacity` | `PositiveIntegerField` | |
| `owner` | `ForeignKey(User)` | `on_delete=CASCADE`, `related_name='venues'` |
| `created_at` | `DateTimeField` | `auto_now_add=True` |

`clean()` calls `validate_venue_capacity` — **blocks lowering `max_capacity`
below capacity already allocated by the venue's events.** Ordering:
`-created_at`.

**`Event`** (`apps/events/models.py`)
| Field | Type | Notes |
|---|---|---|
| `venue` | `ForeignKey(Venue)` | `on_delete=PROTECT` — deleting a venue with events is blocked outright |
| `organizer` | `ForeignKey(User)` | `on_delete=CASCADE`, `related_name='events'` |
| `name` | `CharField(200)` | |
| `description` | `TextField` | blank allowed |
| `date` | `DateTimeField` | |
| `allocated_capacity` | `PositiveIntegerField` | the ticket-budget ceiling for this event |
| `status` | `CharField(20)` | `draft` / `published` / `cancelled`; default `draft` |
| `created_at` | `DateTimeField` | `auto_now_add=True` |
| `public_slug` *(planned)* | `SlugField(unique=True)` | for direct-link/QR access, not yet added |
| `banner_image` *(planned)* | `ImageField` | organizer "canvas" customization, not yet added |
| `accent_color` *(planned)* | `CharField(7)` | hex color, not yet added |
| `scan_token` *(V4)* | `CharField(unique=True)` | regenerable secret for check-in scanning, not yet added |

`clean()` enforces `allocated_capacity <= venue.max_capacity`. Ordering:
`-created_at`.

**`TicketType`** (`apps/tickets/models.py`)
| Field | Type | Notes |
|---|---|---|
| `event` | `ForeignKey(Event)` | `on_delete=CASCADE`, `related_name='ticket_types'` |
| `name` | `CharField(100)` | e.g. "VIP", "General Admission" |
| `price` | `DecimalField(10,2)` | |
| `quantity_total` | `PositiveIntegerField` | |
| `quantity_sold` | `PositiveIntegerField` | default `0` |

Methods:
- `reserved_count` — sum of quantities across **active, non-expired**
  reservations for this tier.
- `available_count` — `quantity_total - quantity_sold - reserved_count`.
- `allocated_total(event)` — classmethod/helper summing `quantity_total`
  across all tiers of an event, used to validate against
  `Event.allocated_capacity`.

`clean()` calls `validate_ticket_type_capacity`, enforcing:
1. `quantity_total >= quantity_sold`.
2. `quantity_total >= quantity_sold + active_held_reservations` (can't shrink
   a tier below what's already committed or held).
3. Sum of all tiers' `quantity_total` for the event `<= Event.allocated_capacity`.

DB-level: `CheckConstraint` enforcing `quantity_sold <= quantity_total` —
this one is a **real DB constraint**, not just an application-layer check
(see Section 9 for where this pattern isn't yet mirrored elsewhere).
Ordering: `price, name`.

**`Reservation`** (`apps/tickets/models.py`)
| Field | Type | Notes |
|---|---|---|
| `ticket_type` | `ForeignKey(TicketType)` | `on_delete=CASCADE`, `related_name='reservations'` |
| `user` | `ForeignKey(User)` | `on_delete=CASCADE`, `related_name='reservations'` |
| `quantity` | `PositiveIntegerField` | |
| `expires_at` | `DateTimeField` | set to `now() + RESERVATION_LOCK_DURATION` on creation |
| `status` | `CharField(20)` | `active` / `expired` / `converted`; default `active` |
| `created_at` | `DateTimeField` | `auto_now_add=True` |

Property: `is_active` → `status == 'active' and expires_at > now()`. This
means a reservation can be *functionally* expired (time has passed) even
before the `expire_reservations` command has formally flipped its `status`
column — availability queries check both, not just the status field.

---

## 4. Concurrency Design

This is the part of the system where correctness matters more than anywhere
else. Two independent concurrency-critical paths exist: **reservation
creation** (V2, built) and **check-in scanning** (V4, designed).

### 4.1 Reservation creation (`apps/tickets/services.py::reserve_tickets`)

```
transaction.atomic():
    tier = TicketType.objects.select_for_update().get(pk=ticket_type_id)
    # reload guarantees we're reading post-lock state, not a stale
    # pre-lock copy fetched before the lock was acquired
    assert tier.event.status == 'published'
    if tier.available_count < requested_quantity:
        raise SoldOut
    Reservation.objects.create(
        ticket_type=tier, user=user, quantity=requested_quantity,
        expires_at=now() + RESERVATION_LOCK_DURATION,  # 10 minutes
        status='active',
    )
```

**Why `select_for_update()` matters here specifically:** without it, two
concurrent requests could both read `available_count = 1` before either
writes a `Reservation`, and both would proceed — a classic
read-then-write race producing an oversold tier. Locking the `TicketType`
row forces the second request to block until the first transaction commits,
so its `available_count` read reflects the first reservation.

**Why the lock is scoped to `TicketType`, not `Event` or `Reservation`:**
locking too broadly (e.g. the whole `Event`) would serialize purchases across
*different* ticket tiers of the same event unnecessarily, hurting throughput
under load with no correctness benefit — different tiers have independent
inventory.

**SQLite-specific consideration:** SQLite locks the entire database file on
any write transaction, not just the touched row — so even with row-level
`select_for_update()` semantics expressed in the ORM, the practical
concurrency ceiling is "one writer at a time" file-wide. The design still
uses `select_for_update()` because (a) it documents intent and (b) it
degrades gracefully to a stronger, still-correct database (e.g. Postgres)
without code changes if this project ever migrates off SQLite. Given that
ceiling, the atomic blocks are kept intentionally short — no email sending,
QR generation, or other I/O happens inside them.

### 4.2 Reservation expiry

Two layers, not one:
1. **Query-time exclusion** — `available_count` and any "is this held"
   check filter out reservations where `expires_at < now()`, regardless of
   whether their `status` column has been updated yet. This means inventory
   is *always* accurate at read time, even between cron runs.
2. **`expire_reservations` management command** — bulk-updates stale
   `active` + past-expiry rows to `status='expired'`. This is for
   operational visibility/reporting (so `active` in the DB actually means
   "currently active," not "was active at some point and might still show up
   in a naive filter") — not a correctness requirement, since layer 1 already
   guarantees correctness independent of when this runs.

### 4.3 Check-in scanning (V4 — designed, not yet built)

```
transaction.atomic():
    ticket = Ticket.objects.select_for_update().get(unique_code=code)
    if ticket.status == 'checked_in':
        return 409  # duplicate
    if ticket.status != 'valid':
        return 400  # refunded / otherwise invalid
    ticket.status = 'checked_in'
    ticket.checked_in_at = now()
    ticket.save()
```

Same pattern as reservation creation: lock the row being mutated, check
state *after* acquiring the lock (not before), mutate, commit. For batch
scans, each code gets its **own** atomic block so one failure in a batch of
50 doesn't roll back the other 49 — the response reports per-code results
independently.

### 4.4 Waitlist promotion (V5 — designed, not yet built)

Promotion happens **inside the same transaction as the refund**, not via a
decoupled async signal/task. This is deliberate: if promotion were
async (e.g. a Celery task fired after the refund commits), there's a window
where the freed inventory is visible to both the waitlist-promotion job *and*
a concurrent ordinary purchase — whichever wins the race gets the slot, and
the other fails confusingly. Doing it synchronously inside the refund's
`atomic()` block means the slot is never actually "free" from an outside
observer's perspective — it goes directly from "held by refunded ticket" to
"held by promoted waitlist reservation" without an visible gap.

```
transaction.atomic():
    # (already inside the refund's transaction)
    tier = TicketType.objects.select_for_update().get(pk=...)
    entry = WaitlistEntry.objects.select_for_update(
        skip_locked=True
    ).filter(ticket_type=tier, status='waiting').order_by('joined_at').first()
    if entry:
        Reservation.objects.create(
            ticket_type=tier, user=entry.user, quantity=1,
            expires_at=now() + RESERVATION_LOCK_DURATION, status='active',
        )
        entry.status = 'promoted'
        entry.save()
        # notify (stub) — outside any lock-sensitive path
```

`skip_locked=True` is used on the waitlist query (not the tier query) so
that if two refunds for the *same tier* happen concurrently, they don't
deadlock each other waiting on the same waitlist row — each grabs a
different waiting entry if one is available, rather than blocking.

---

## 5. Request Flow Diagrams (textual)

### 5.1 Attendee ticket purchase (V2, as built — reservation stage only)

```
GET  /events/<id>/                 → public event detail, shows ticket tiers
POST /tickets/add/                 → AddToCartForm validates tier + quantity
                                       → reserve_tickets() [atomic, locked]
                                       → Reservation(status=active, expires_at=+10m)
GET  /tickets/cart/                → shows active reservations for request.user
POST /tickets/remove/<id>/         → cancels a reservation early
```
*(Checkout → payment → `Ticket` conversion is V3 scope, not yet built.)*

### 5.2 Organizer event setup (V1+V2, as built)

```
POST /events/venues/create/        → Venue(owner=request.user)
POST /events/create/                → EventForm(venue queryset scoped to owner)
                                       → Event(organizer=request.user, status=draft)
POST /tickets/types/create/         → TicketTypeForm
                                       → TicketType(event=<owned event>)
                                       → clean() validates against
                                         Event.allocated_capacity
POST /events/<id>/publish/          → status: draft → published
```

### 5.3 Check-in scan (V4, designed)

```
GET  /checkin/<scan_token>/          → scan page, no login
                                        → prompts scanner_name once (session)
POST /checkin/<scan_token>/scan/     → { "codes": [...] }
                                        → per-code atomic check + mark checked_in
                                        → CheckInLog entry per attempt
                                        → response: per-code results
```

---

## 6. Security Design Notes

- **No DRF anywhere** — endpoints are explicit `JsonResponse` views, keeping
  the attack surface and dependency footprint minimal and fully auditable.
- **Ownership checks happen at the queryset level**, never only in templates
  — see Section 2. A missing `owner=request.user` filter is treated as a bug
  class, not a UI nicety.
- **404, not 403, for cross-ownership access** on organizer resources (e.g.
  editing another organizer's venue) — avoids confirming an object's
  existence to a user who shouldn't know about it.
- **`scan_token` (V4) is the sole auth for check-in** — no session, no
  password. Its security properties: high-entropy generation (same
  `secrets.token_urlsafe` approach as ticket codes), instantly revocable by
  regeneration, and scoped to exactly one event (compromise of one event's
  link doesn't expose others).
- **CSRF:** standard Django CSRF protection applies to all state-changing
  views. The V4 scan endpoint is the one place this needs explicit design
  attention once built — a token-only-authenticated POST from a scan device
  still needs CSRF handling (likely via a CSRF-exempt view guarded instead by
  the unguessable token itself, since the device has no Django session to
  carry a CSRF cookie in the typical case — this trade-off will be finalized
  when V4 starts).

---

## 7. Template & Rendering Design

- All app-facing templates under `templates/jinja2/<app_name>/`, rendered by
  Django's built-in Jinja2 backend (`config/jinja2.py` registers `url` and
  `static` as Jinja2 globals, since Jinja2 doesn't get Django's template tags
  automatically).
- `django.contrib.admin` is the **only** consumer of the Django template
  engine — no app view renders through it.
- Organizer views and public/attendee views are template-separated by
  directory convention (not currently enforced by a naming scheme beyond
  folder placement — worth formalizing if the template count grows).

---

## 8. Testing Strategy (as implemented)

- **`apps/events/tests.py`** — capacity constraint enforcement (model +
  signal layer), organizer ownership/access scoping, public visibility
  restricted to `published` events.
- **`apps/tickets/tests.py`** — capacity invariants, reservation lifecycle,
  cart flow, the `expire_reservations` command, and concurrency/oversell
  protection specifically (i.e. tests that simulate the race condition
  `select_for_update()` is meant to prevent, not just the happy path).
- **`apps/accounts/tests.py`** — profile auto-creation, signup role
  persistence, login, and the authenticated-user-hits-signup redirect edge
  case.

---

## 9. Known Gaps vs. Design (current implementation state)

| Gap | Impact | Priority |
|---|---|---|
| `Event.public_slug` not implemented | No clean direct-link/QR entry point for attendees yet — only dashboard access works | Should land before/alongside V3, since ticket QR generation and direct event links are conceptually related |
| `Event.banner_image` / `accent_color` not implemented | Organizer "canvas" customization from the design discussion isn't buildable yet | Low urgency — cosmetic, can land anytime before V1 is considered fully closed |
| Event/Venue capacity backstop is `pre_save`-signal-only, not a DB `CheckConstraint` | Protects all normal ORM `.save()` paths (which is the entire application surface today) but not `bulk_update()` or raw SQL | Low risk given current codebase has no bulk-update paths on these models; revisit if that changes |
| `scan_token` not implemented | Expected — this is V4 scope | N/A, on schedule |

---

## 10. Open Design Decisions for Upcoming Versions

- **V3:** exact QR image storage strategy — file-per-ticket under
  `MEDIA_ROOT/qr/` vs. generating on-demand at request time (storing only
  the token). Current plan favors stored files for reusability (re-sending a
  ticket email later without regenerating).
- **V4:** CSRF handling for the token-authenticated scan endpoint (see
  Section 6) — needs a final decision when V4 starts.
- **V5:** notification mechanism for waitlist promotion — currently
  specified as a "stub" (log/console), real email/SMS integration not yet
  scoped.
