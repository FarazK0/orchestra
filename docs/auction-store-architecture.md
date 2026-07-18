# Online Auction Store — Architecture

A single-seller or multi-seller auction platform where users can list items, place bids in real time, and settle payments automatically when an auction closes.

---

## Core domain model

| Entity | Key fields | Notes |
|---|---|---|
| `User` | id, email, password_hash, stripe_customer_id, role (buyer/seller/admin) | Buyers and sellers share one account |
| `Listing` | id, seller_id, title, description, start_price, reserve_price, min_increment, start_at, end_at, status | Images stored separately in object storage |
| `ListingImage` | id, listing_id, storage_key, position | Up to 10 images per listing |
| `Bid` | id, listing_id, bidder_id, amount, placed_at | Append-only; never updated or deleted |
| `AuctionClose` | id, listing_id, winner_id, winning_bid_id, closed_at | Written atomically when timer expires |
| `Payment` | id, auction_close_id, stripe_payment_intent_id, amount, status | Stripe captures after close |
| `Notification` | id, user_id, type, payload, read_at | Outbid, won, listing ending soon, payment success |

---

## Auction status machine

```
draft ──► active ──► ending ──► closed ──► settled
  │          │                    │
  └──────────┴──► cancelled       └──► failed (payment failed)
```

- `draft` — seller created listing, start_at is in the future
- `active` — start_at reached, bids accepted
- `ending` — last 5 minutes; any new bid extends the timer by 5 minutes (anti-sniping)
- `closed` — timer expired; no more bids; winner locked
- `settled` — Stripe payment captured; item sold
- `cancelled` — seller cancelled before any bids, or reserve not met at close

Every status transition writes an append-only `listing_events` row (type, actor, timestamp, payload). This is the audit trail.

---

## System architecture

```
Browser / Mobile
      │
      ├── HTTPS REST   ──► API Service (FastAPI)  ──► Postgres (canonical data)
      │                          │                         │
      └── WebSocket   ──► WS Gateway               Redis (hot auction state +
                               │                    pub/sub for real-time events)
                               └── subscribes to Redis pub/sub

Background workers
  ├── Auction Closer  (polls Redis TTL expiry or cron)
  ├── Payment Worker  (captures Stripe payment after close)
  └── Notifier        (sends email / push on bid events)

External services
  ├── Stripe           (payment intents, webhooks)
  ├── Cloudflare R2    (item images, resized thumbnails)
  └── SendGrid         (transactional email)
```

---

## Storage split

### Postgres (canonical, durable)
Tables: `users`, `listings`, `listing_images`, `listing_events`, `bids`, `auction_closes`, `payments`, `notifications`

The bid table is append-only. The current winning bid is always `SELECT MAX(amount) FROM bids WHERE listing_id = ?` — but this is cached in Redis so it is never hit per-request.

### Redis (hot state, fast reads)
| Key | Value | Purpose |
|---|---|---|
| `auction:{listing_id}:high_bid` | `{amount, bidder_id, bid_id}` JSON | Current winning bid, read on every page load |
| `auction:{listing_id}:end_at` | Unix timestamp | Authoritative close time (updated on snipe extension) |
| `auction:{listing_id}:ttl` | Redis TTL key | Expiry triggers close worker via keyspace notification |
| `auction:{listing_id}:events` | pub/sub channel | New bids and timer extensions broadcast to WebSocket subscribers |

Redis is the read path; Postgres is the write path. On auction close the worker reconciles Redis state into `auction_closes`.

### Cloudflare R2 (object storage)
Raw uploads + CDN-served thumbnails (resized at upload time by a worker). Signed URLs for upload; public CDN URLs for read.

---

## Bid flow (critical path)

```
1. User submits bid amount via POST /listings/{id}/bids
2. API validates:
     - User authenticated, not the seller
     - Listing status in (active, ending)
     - amount >= current_high_bid + min_increment
     - amount >= reserve_price (optional soft check)
3. BEGIN TRANSACTION
     - INSERT bid row
     - UPDATE listing_events (append bid_placed event)
4. COMMIT
5. Update Redis: SET auction:{id}:high_bid
6. If now in ending window AND bid just placed:
     - Extend end_at by 5 min in Redis + Postgres
7. PUBLISH to Redis pub/sub channel auction:{id}:events
8. Return 201 to bidder
9. WebSocket gateway broadcasts new_bid event to all subscribers of listing
10. Notifier queues outbid email to previous high bidder
```

The Postgres write (steps 3-4) is the single source of truth. Redis is updated after commit. If Redis update fails, a reconciliation job reads Postgres on next close.

---

## Auction close flow

```
1. Redis TTL for auction:{id} expires
   → keyspace notification fires
2. Auction Closer worker picks it up
3. Fetches highest bid from Postgres (reconfirm, not Redis)
4. BEGIN TRANSACTION
     - INSERT auction_closes row (winner, winning_bid, closed_at)
     - UPDATE listing status = 'closed'
     - INSERT listing_events row (auction_closed)
5. COMMIT
6. Payment Worker: create Stripe PaymentIntent for winner
     - Capture payment
     - On success: UPDATE payment status = 'captured', listing status = 'settled'
     - On failure: mark payment failed, notify seller, optionally offer to next bidder
7. Notify winner (won) and losing bidders (lost)
```

Idempotency: the `auction_closes` table has a unique constraint on `listing_id`, so a double-fire of the close worker fails harmlessly on the second attempt.

---

## Real-time WebSocket protocol

Clients connect to `wss://api/ws/auctions/{listing_id}` after authenticating.

Server → client message types:

```json
{ "type": "new_bid",      "amount": 150.00, "bidder": "u_xxx", "end_at": 1720000000 }
{ "type": "timer_extend", "end_at": 1720000300 }
{ "type": "auction_closed", "winner": "u_xxx", "winning_amount": 150.00 }
{ "type": "heartbeat" }
```

Client → server: `ping` only. All writes go through REST.

Multiple WebSocket server instances subscribe to the same Redis pub/sub channel — horizontal scaling is free.

---

## API surface (REST)

| Method | Path | Description |
|---|---|---|
| POST | `/auth/register` | Create account |
| POST | `/auth/login` | Issue JWT + refresh token |
| POST | `/auth/refresh` | Refresh JWT |
| GET | `/listings` | Browse/search (filter by status, category, price) |
| GET | `/listings/{id}` | Listing detail + current high bid |
| POST | `/listings` | Create listing (seller) |
| PATCH | `/listings/{id}` | Edit draft listing |
| DELETE | `/listings/{id}` | Cancel listing (no bids) |
| POST | `/listings/{id}/images` | Upload image (returns signed R2 URL) |
| GET | `/listings/{id}/bids` | Bid history |
| POST | `/listings/{id}/bids` | Place bid |
| GET | `/users/me` | My profile |
| GET | `/users/me/listings` | My listings (selling) |
| GET | `/users/me/bids` | My bid history |
| GET | `/users/me/wins` | Auctions I won |
| GET | `/notifications` | My notifications |
| PATCH | `/notifications/{id}/read` | Mark read |
| POST | `/webhooks/stripe` | Stripe webhook receiver |

---

## Frontend pages

```
/                   — homepage: featured + ending-soon listings
/listings           — browse with filters (category, price, time left)
/listings/{id}      — listing detail, bid form, real-time countdown, bid history
/sell               — create listing form (drag-drop images, set reserve, schedule)
/dashboard          — seller: my listings, active bids on them, revenue
/bids               — buyer: items I've bid on, current status
/wins               — items I won, payment status
/notifications      — all alerts
/account            — profile, payment methods, address
```

Tech: Next.js (App Router, SSR for listing pages for SEO + fast first load, CSR for real-time bid UI).

---

## Tech stack

| Layer | Choice | Reason |
|---|---|---|
| Frontend | Next.js 15 + Tailwind | SSR for SEO, App Router for streaming, easy deployment |
| API | FastAPI (Python 3.12) | Async, fast, OpenAPI docs out of the box |
| Database | Postgres 16 | ACID for bids and payments; row-level locking on bid insert |
| Cache / pub-sub | Redis 7 | Hot auction state; keyspace notifications for close trigger; pub/sub for WS fan-out |
| Object storage | Cloudflare R2 | S3-compatible, no egress fees, built-in CDN |
| Payments | Stripe | PaymentIntents API; webhooks for async capture confirmation |
| Email | SendGrid | Transactional templates; good deliverability |
| Auth | JWT (short-lived) + refresh tokens in httpOnly cookie | Stateless API; refresh rotation for security |

---

## Security

- All bid amounts validated server-side; client amount is untrusted input
- Stripe Elements for card capture — card data never touches the server
- Stripe webhook signature verified before processing any payment event
- Image uploads: MIME type check, 10 MB size cap, virus scan via ClamAV or SaaS
- Rate limiting on `POST /listings/{id}/bids`: 10 req/min per user per listing
- CSRF protection: SameSite=Strict cookies + CSRF token for state-changing requests
- JWT secret rotated via environment variable; refresh tokens stored hashed in DB

---

## Data flow diagram

```
Seller
  │ POST /listings
  ▼
API ──► Postgres (listing row, status=draft)
  │
  │ start_at reached
  ▼
Background job ──► UPDATE listing status=active
                ──► SET Redis TTL = end_at

Buyer
  │ POST /listings/{id}/bids
  ▼
API ──► validate ──► Postgres INSERT bid
                 ──► Redis SET high_bid
                 ──► Redis PUBLISH new_bid
                         │
                         ▼
                  WebSocket gateway ──► all browser tabs watching listing

Redis TTL expiry
  │
  ▼
Auction Closer ──► Postgres INSERT auction_closes
Payment Worker ──► Stripe capture ──► Postgres UPDATE payment
Notifier       ──► SendGrid email to winner + losers
```

---

## Scaling path

| Concern | Approach |
|---|---|
| Read-heavy listing browse | Postgres read replica + aggressive HTTP caching (CDN for listing pages) |
| High bid volume on popular items | Redis absorbs reads; Postgres row lock on `listings.id` serialises writes per listing |
| Many concurrent WebSocket clients | Multiple WS servers, all subscribing to same Redis channel |
| Slow image uploads | Pre-signed R2 upload URL — browser uploads directly to R2, API never proxies bytes |
| Auction close at exact moment | Redis keyspace TTL notification + idempotent DB constraint prevents double-close |

---

## Open questions / deferred

- **Proxy bidding (autobid):** place max bid, system auto-increments to stay winning up to max — adds complexity to bid validation loop
- **Reserve price visibility:** show "reserve not met" vs. hide reserve — UX decision
- **Buyer protection / escrow:** hold payment in escrow until buyer confirms receipt
- **Dispute flow:** seller doesn't ship, buyer opens dispute — needs a state machine extension
- **Mobile app:** same REST + WS API works; React Native or Flutter client
- **Multi-currency:** single currency MVP; Stripe multi-currency is additive
