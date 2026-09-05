<div align="center">

# 🎟️ TickIt — Event Ticketing & QR Validation Platform

**A secure, concurrency-safe event ticketing platform with cart-based holds, per-ticket QR code generation, real-time check-in, and bulk VIP import.**

Built with **Django 6.1**, **Jinja2**, **SQLite**, **Celery + Redis**, and a write-lock-first concurrency model — no overselling, no double-checkout, ever.

[![Python](https://img.shields.io/badge/Python-3.14-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Django](https://img.shields.io/badge/Django-6.1-092E20?logo=django&logoColor=white)](https://www.djangoproject.com/)
[![Tests](https://img.shields.io/badge/tests-61%20passing-brightgreen)](#testing)
[![License](https://img.shields.io/badge/license-MIT-blue)](#license)

</div>

---

## 💡 The Project Idea

Most DIY ticket sellers rely on spreadsheets, and on big days the race to grab tickets turns into a free-for-all: the cart shows a seat, the buyer pays, and somehow three people bought the same spot. **TickIt** fixes that by treating ticket inventory like a real-world reservation system.

The platform lets **organizers** publish events with tiered pricing, and lets **attendees** hold tickets in a cart before buying. Every hold is time-limited and concurrency-safe, so availability is always accurate — even when hundreds of buyers hammer the same event at once. On purchase, each ticket is minted with a **unique cryptographic code** and a **QR code image**, ready to be scanned at the door. Organizers can check in attendees one at a time or via a bulk QR scanner, and can bulk-import VIP guests by CSV.

### 🎯 Core principles

- **No overselling, ever** — availability is computed from `sold + active holds`, not just `sold`.
- **Write-lock-first concurrency** — SQLite has no row locks, so we take the database write lock *before* reading availability to serialize competing buyers.
- **One ticket = one admission** — a cart of quantity N produces N individually-coded tickets, each with its own QR.
- **Enforced capacity chain** — venue capacity ≥ event allocation ≥ tier allocations, validated at the model level in both directions.

---

## ✨ Features

### 👤 Accounts (`apps.accounts`)
- Signup / login / logout with Django's hardened auth.
- Role-based profiles: **Organizer** or **Attendee**.
- `organizer_required` decorator guards all organizer-only views.

### 📅 Events & Venues (`apps.events`)
- Organizers manage venues (with capacity) and events (draft / published / cancelled).
- Capacity chain enforced: an event can't exceed its venue, and a venue can't be shrunk below an existing event's allocation.
- Public browse shows only **published** events.
- **Waitlist** — attendees can join a waitlist when an event is sold out; entries are promoted automatically when tickets are refunded.
- **Bulk VIP import** — organizers can upload a CSV (`email`, `full_name`, `ticket_type`) to pre-register VIP guests. The importer creates inactive placeholder accounts and allocates tickets directly, skipping duplicate or invalid rows with per-row error reporting.

### 🛒 Tickets, Cart & Holds (`apps.tickets`)
- **Ticket tiers** — organizers define multiple price tiers per event with per-tier quantities.
- **Cart with 10-minute holds** — reserving tickets locks them for 10 minutes; an `expire_reservations` management command releases expired holds.
- **Concurrency-safe reservations** — threaded stress tests prove only one of four simultaneous buyers gets the last ticket.
- **Capacity guardrails** — a tier can't be trimmed below `sold + active holds`, and tier totals can't exceed event capacity.

### 🧾 Checkout & QR Codes
- **Atomic checkout** — converts every active cart hold into real `Ticket` records inside a single transaction; a duplicate click or an expired hold is rejected, not double-charged.
- **Unique ticket codes** — `secrets.token_urlsafe(24)` with a collision loop.
- **Per-ticket QR PNGs** — generated with `qrcode`, stored under `media/qr/`, viewable on each ticket's detail page.
- **My Tickets** — attendee dashboard listing every purchased ticket with status and QR.

### ✅ Event Check-In
- **Single check-in** — organizers scan or enter a QR code to mark a ticket as used.
- **Bulk check-in** — scan multiple QR codes in one session; results show per-ticket status in real time.

### ⚙️ Background Tasks (Celery + Redis)
- Celery is wired up with Redis as both broker and result backend.
- Used for async processing of waitlist promotions and refund handling (`apps/events/tasks.py`).
- Management command `expire_reservations` handles hold cleanup and can be scheduled via cron or Celery Beat.

---

## 🧱 Architecture

```
ticketing_platform/
├── manage.py
├── requirements.txt
├── celery.py                # Celery app configuration
├── config/                  # project config
│   ├── settings.py          # SQLite, dual template engines, media, Celery, test DB
│   ├── urls.py              # includes apps + dev media serving
│   └── jinja2.py            # Jinja2 environment with url()/static globals
├── apps/
│   ├── accounts/            # Profile (roles), signup/login/logout, decorators
│   ├── events/              # Venue, Event, WaitlistEntry; bulk import; check-in views
│   └── tickets/             # TicketType, Reservation, Ticket + services
├── templates/jinja2/        # Jinja2 templates
└── static/css/              # styles.css — single stylesheet
```

### The concurrency trick

SQLite's `select_for_update()` is a no-op, so on SQLite the file-level write lock is only taken at **commit time** — meaning two concurrent reservations could both read stale availability and oversell. TickIt works around this:

```python
with transaction.atomic():
    # 1. No-op UPDATE takes the SQLite RESERVED write lock immediately,
    #    serializing concurrent writes on the same tier.
    TicketType.objects.filter(pk=ticket_type_id).update(
        quantity_sold=F("quantity_sold")
    )
    # 2. Only now is it safe to read availability and reserve.
    ticket_type = TicketType.objects.select_for_update().get(pk=ticket_type_id)
    ...
```

The same pattern is used in `checkout_cart` — lock first, then convert holds → tickets.

### Data model

| Model | Notes |
|---|---|
| `Profile` | OneToOne to `auth.User`; `role` ∈ organizer / attendee |
| `Venue` | `max_capacity`, owned by an organizer |
| `Event` | `allocated_capacity` ≤ venue capacity; status draft/published/cancelled |
| `TicketType` | per-event pricing tier; `quantity_total` / `quantity_sold` |
| `Reservation` | cart hold; `quantity`, `expires_at` (10 min), status active/expired/converted |
| `Ticket` | `unique_code` (unique), `qr_image`, status active/used/refunded/cancelled, `purchased_at` |
| `WaitlistEntry` | email + optional user FK; `position`, status waiting/promoted |

---

## 🚀 Getting Started

### Prerequisites
- Python **3.14**
- **Redis** running locally on port 6379 (required for Celery; install via your OS package manager or `docker run -p 6379:6379 redis`)

### 1. Clone & set up

```bash
git clone https://github.com/shlok-angale/Tick-It.git
cd Tick-It/ticketing_platform

python -m venv .venv

# macOS/Linux:
source .venv/bin/activate
# Windows:
.venv\Scripts\activate

pip install -r requirements.txt
```

### 2. Prepare the database

```bash
python manage.py migrate
python manage.py createsuperuser   # e.g. admin / admin12345
```

### 3. Run the development server

```bash
python manage.py runserver
```

Visit **http://127.0.0.1:8000/** — sign up, choose the **Organizer** role, create a venue + published event + ticket tiers, then log in as an attendee to hold tickets and check out.

### 4. Run the Celery worker (optional — required for async task processing)

In a separate terminal, with the virtualenv active:

```bash
celery -A ticketing_platform worker --loglevel=info
```

### 5. Expire stale holds

Run manually or schedule via cron / Celery Beat:

```bash
python manage.py expire_reservations
```

### 6. Bulk import VIP guests

Prepare a CSV with the following columns:

```
email,full_name,ticket_type
alice@example.com,Alice Johnson,GA
bob@example.com,Bob Smith,VIP
```

`ticket_type` is optional — if omitted, the first available tier is used. Upload via the **Bulk Import VIP Guests** button on the organizer event detail page.

---

## 🧪 Testing

```bash
# macOS/Linux:
python manage.py test

# Windows — remove stale test DB first (file-lock quirk):
Remove-Item test_db.sqlite3 -ErrorAction SilentlyContinue
python manage.py test
```

**61 tests** — covering the capacity chain, tier guardrails, concurrency stress (4 threads racing for the last ticket), checkout conversion, duplicate-checkout protection, expired-hold rejection, QR generation, waitlist promotion, and view flows. The test suite uses a real file-based SQLite DB so threaded tests genuinely exercise locking.

---

## 🛠️ Tech Stack

| Library | Version | Purpose |
|---|---|---|
| Django | 6.1 | ORM, auth, admin, migrations, views |
| Jinja2 | 3.1.6 | Template engine for all app templates |
| qrcode | 8.2 | QR code PNG generation |
| Pillow | 12.3.0 | Image processing (required by qrcode) |
| Celery | 5.6.3 | Async task queue |
| Redis | 8.1.0 | Celery broker & result backend |
| SQLite | — | Lightweight single-file database |

---

## 📄 License

MIT
