# Architecture

## System overview

Three services, one database. The React frontend talks exclusively to the Python FastAPI backend. The Python backend proxies quote and hold requests to the Java service, and the Java service calls back to Python on `/internal/bookings/from-hold` when a hold is confirmed. SQLite lives on the Python side; the Java service has its own H2/SQLite store for quotes, holds, and audit events.

```mermaid
graph TD
    FE["React frontend<br/>:5173"]

    subgraph Python ["Python FastAPI :8001"]
        REST["REST API<br/>/flights, /bookings,<br/>/waitlist, /users"]
        PROXY["Hold proxy<br/>/quotes, /holds"]
        INTERNAL["/internal/bookings/from-hold"]
        DB[("SQLite<br/>booking.db")]
    end

    subgraph Java ["Java Spring Boot :8080"]
        HAPI["Hold API<br/>/api/v1/quotes<br/>/api/v1/holds"]
        HDB[("SQLite/H2<br/>holds.db")]
    end

    FE -->|"REST"| REST
    FE -->|"REST"| PROXY
    REST --- DB
    INTERNAL --- DB
    PROXY -->|"HTTP proxy"| HAPI
    HAPI --- HDB
    HAPI -->|"POST /internal/bookings/from-hold<br/>(on confirm)"| INTERNAL
```

---

## Atomic seat claim — two concurrent booking requests

The seat decrement is a single `UPDATE … WHERE seats > 0`. SQLite serialises writers at the file level, so only one of the two concurrent requests can match the `WHERE` clause; the other sees `rowcount == 0` and gets `NO_SEATS_AVAILABLE`. This eliminates the read-modify-write race where both sessions read the same seat count before either commits.

```mermaid
sequenceDiagram
    participant A as Request A
    participant B as Request B
    participant DB as SQLite

    par simultaneous requests
        A->>DB: UPDATE flights SET seats = seats - 1<br/>WHERE flight_id = 1 AND economy_seats_available > 0
    and
        B->>DB: UPDATE flights SET seats = seats - 1<br/>WHERE flight_id = 1 AND economy_seats_available > 0
    end

    Note over DB: SQLite serialises writes.<br/>One UPDATE matches, the other sees rowcount=0.

    DB-->>A: rowcount = 1 ✓
    A->>DB: INSERT booking (status='booked')
    A->>DB: COMMIT
    A-->>A: BookingOut

    DB-->>B: rowcount = 0
    B-->>B: ErrorResponse NO_SEATS_AVAILABLE
```

---

## Cancel + waitlist promotion in one transaction

The cancel, the seat restore, and the waitlist promotion all commit together. The promotion runs inside a savepoint so that if it fails unexpectedly, only the promotion is rolled back — the cancel still commits. The waiter claim (`waiting → promoting`) uses the same conditional UPDATE pattern as the seat decrement.

```mermaid
sequenceDiagram
    participant C as cancel_booking()
    participant DB as SQLite
    participant P as attempt_promotion()

    C->>DB: BEGIN (implicit)

    C->>DB: UPDATE bookings SET status='cancelled'<br/>WHERE booking_id=? AND status='booked'
    Note over DB: rowcount=0 → ALREADY_CANCELLED (return early)
    DB-->>C: rowcount = 1 ✓

    C->>DB: UPDATE flights SET seats = seats + 1<br/>WHERE flight_id = ?

    C->>DB: SAVEPOINT sp1
    C->>P: attempt_promotion(flight_id, seat_class)

    P->>DB: SELECT MIN(waitlist_id) WHERE status='waiting'
    DB-->>P: next_id = 42

    P->>DB: UPDATE waitlist SET status='promoting'<br/>WHERE waitlist_id=42 AND status='waiting'
    DB-->>P: rowcount = 1 (claimed)

    P->>DB: UPDATE flights SET seats = seats - 1<br/>WHERE flight_id=? AND seats > 0
    DB-->>P: rowcount = 1 ✓

    P->>DB: INSERT booking for waiter (no commit)
    P->>DB: UPDATE waitlist SET status='booked' WHERE waitlist_id=42

    Note over C,P: Savepoint released (success path)
    C->>DB: COMMIT
    Note over DB: All writes land atomically:<br/>booking cancelled + seat restored +<br/>waiter booked + waitlist updated
```

---

## Java hold lifecycle

The `CONFIRMING` state is the key concurrency guard: only one caller can win the `HELD → CONFIRMING` UPDATE because the `WHERE status = HELD` clause serialises concurrent confirm attempts. On Python failure the hold reverts to `HELD` so the caller can retry. A background scheduler expires `HELD` holds that have passed their `reservedUntil` timestamp.

```mermaid
stateDiagram-v2
    [*] --> HELD : createHold()

    HELD --> CONFIRMING : claimForConfirm()<br/>UPDATE WHERE status=HELD<br/>(only one caller wins)
    HELD --> EXPIRED : scheduler bulk UPDATE<br/>WHERE reservedUntil before now
    HELD --> RELEASED : releaseHold()

    CONFIRMING --> CONFIRMED : Python booking created ✓
    CONFIRMING --> HELD : Python call failed<br/>(revert so caller can retry)

    CONFIRMED --> [*]
    EXPIRED --> [*]
    RELEASED --> [*]
```

---

## Waitlist entry lifecycle

A partial unique index (`WHERE status = 'waiting'`) enforces at most one active entry per `(user_id, flight_id, seat_class)` while still allowing users to rejoin after a previous booking or cancellation. The two failure paths are handled differently: a `NO_SEATS_AVAILABLE` result resets the entry to `waiting` (the seat raced away, the user keeps their place), while any other unexpected error marks it `failed` so the queue can advance to the next waiter.

```mermaid
stateDiagram-v2
    [*] --> waiting : join_waitlist()

    waiting --> promoting : attempt_promotion()<br/>UPDATE WHERE status='waiting'<br/>(atomic claim by MIN waitlist_id)
    waiting --> cancelled : leave_waitlist()<br/>UPDATE WHERE status='waiting'

    promoting --> booked : seat secured +<br/>booking created
    promoting --> waiting : NO_SEATS_AVAILABLE<br/>(seat grabbed by direct booker;<br/>user keeps queue position)
    promoting --> failed : unexpected error<br/>(skip; try next waiter)

    booked --> [*]
    cancelled --> [*]
    failed --> [*]
```
