# Acacia — Agentic Personal Finance Tracker

A Telegram bot you text like a person, not an app you fill in forms for. Send it a
receipt photo, a voice note, or just type what you spent — an LLM turns it into a
structured transaction and writes it to Postgres. Send it a credit card statement
(PDF or photo, single card or a combined multi-card statement) and it extracts miles
earned, current balance, and — where the bank states it — exactly when a batch of
points expires, estimating the rest with a documented FIFO model. Ask it questions
in plain English about your own spending, or tell it to fix a transaction you logged
wrong, and it just does it. A local web dashboard visualizes all of it.

## What it does

**Transaction logging** — send a photo, voice note, or text describing a purchase.
The bot extracts merchant/amount/date/category, lets you review and edit any field
before saving (via buttons or by just typing a correction), then asks which card you
paid with. Multiple receipts sent as a Telegram album are queued and reviewed one at
a time; a photo is auto-classified as a receipt vs. a statement before anything else
happens to it.

**Statement parsing** — upload a statement as PDF or photo(s). Handles combined
statements covering multiple cards, matches each card to your saved payment methods
by last-4 digits (asking if it doesn't recognize one), and extracts: miles/points
earned this cycle, current total balance, and any bank-stated "expiring soon" flag
(e.g. DBS's "Expiring On" column). A photo *album* of several statements from
*different* banks is detected and split correctly — each bank's pages get grouped
and extracted separately, even if you mix a DBS and a Citi statement in one send.

**Miles expiry estimate** — each card's actual expiry rule (verified against the
issuer's T&Cs, not guessed) drives a FIFO-style projection of when each earned batch
expires: DBS Altitude never expires, DBS World Woman's expires 1 year after the
earning quarter, HSBC TravelOne expires 37 months after the earning month, Citi
Rewards expires in 60-month account-anchored cycles. Always labeled as an estimate —
redemptions made outside this system aren't visible here, so it can't reflect a true
remaining balance. Every card's expiry-rule "last verified" date is tracked, and a
stale one (unchecked in 6+ months) gets flagged in the weekly digest.

**Budgeting** — `/budget` (or the bot asks proactively near month-end) walks you
through per-category budgets, a single overall budget, or opting out entirely.
Per-category mode supports an optional overall spending cap that must be ≥ the sum
of category budgets. At month-end the bot asks whether to carry the budget over
unchanged or redefine it — if you don't answer, it holds last month's numbers as a
provisional default (so safe-to-spend doesn't go dark) while continuing to ask.

**Recurring-charge detection** — after logging a transaction, the bot watches for
the same merchant/amount recurring 2+ times in ~90 days (fuzzy-matched, so "Netflix"
and "NETFLIX SG" are recognized as the same thing) and asks if it's a subscription.
Say yes and pick a frequency (or a custom one); say no and it won't ask again for
that merchant. `/recurringexpense` lets you register one manually, including its
actual charge date, without waiting for auto-detection.

**Safe-to-spend** — a daily message: remaining budget minus upcoming recurring
charges, divided by days left in the month, with a full per-category breakdown
inline. Explicit about what's included in the number, not a black box. Gracefully
declines to compute anything (and offers to start the budget flow) if no budget is
set.

**Natural language queries & corrections** — ask things like "how much did I spend
on Grab last month" or "am I overspending on transport" and the bot calls
parameterized, read-only query functions (never raw SQL) to answer with real data —
it says so honestly if nothing matches rather than guessing a number. Follow-up
questions ("what about food?") are understood in context via a short rolling
conversation history. Say "change capcut's category to bills and subscriptions" and
it finds the transaction, shows you the before/after, and waits for a confirm button
before writing anything.

**Weekly insights digest** (Mondays, 8am SGT) — this week's total spend and biggest
category, budget alerts at 50/70/90% usage tiers, miles expiring within 60 days, and
a nudge if any card's expiry rule hasn't been re-verified in 6+ months.

**Dashboard** — a local Flask app: spending by category and by card (this month),
total miles per card, bank-flagged expiries, and a 12-month miles-expiry timeline
with a "View All" page for the unfiltered version.

## Stack

- **Interface**: Telegram bot (`python-telegram-bot`, long-polling, runs locally)
- **LLM**: OpenAI API — `gpt-4.1-mini` for all extraction/classification/query
  tool-calling (vision + text, `temperature=0` for deterministic structured output),
  `gpt-4o-mini-transcribe` for voice notes
- **Storage**: Supabase (Postgres) — backend uses the `service_role` key directly
  (RLS is on with no permissive policies; there's no Supabase Auth layer since this
  is a single-user app, so the trusted backend bypasses RLS rather than working
  around it)
- **Scheduling**: `python-telegram-bot`'s JobQueue (APScheduler) — one daily job
  (8am SGT: budget check + safe-to-spend) and one weekly job (Monday 8am SGT: insights)
- **Dashboard**: Flask + Jinja2 + Chart.js, reusing the same `services/` modules as
  the bot (no duplicated query logic)

## Project layout

```
bot/         Telegram handlers, per-feature modules (budget, recurring, safe-to-spend,
             insights, corrections), config/env loading, date/amount parsing helpers
services/    OpenAI extraction/classification/tool-calling, Supabase client,
             budgets, miles/FIFO expiry math, recurring-charge detection,
             safe-to-spend calculation, NL query tools, dashboard data aggregation
dashboard/   Flask app + templates
db/          schema.sql — full current schema + seed data
```

## Setup

1. Create and activate a virtualenv:
   ```bash
   python -m venv .venv
   .venv\Scripts\activate      # Windows
   ```
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Copy `.env.example` to `.env` and fill in real values (see table below).
4. Run `db/schema.sql` in Supabase's SQL Editor to create all tables and seed data.
5. Start the bot:
   ```bash
   python -m bot.main
   ```
6. Start the dashboard (separate terminal):
   ```bash
   python -m dashboard.app
   ```
   Open http://127.0.0.1:5000

## Env vars

| Variable | Notes |
|---|---|
| `TELEGRAM_BOT_TOKEN` | From @BotFather |
| `TELEGRAM_ALLOWED_USER_ID` | Restricts the bot to you; **required** for all proactive/scheduled messages (daily check, weekly insights) — those jobs silently skip without it |
| `OPENAI_API_KEY` | Used for every extraction, classification, and query call |
| `SUPABASE_URL` | Your Supabase project URL |
| `SUPABASE_KEY` | Publishable/anon key (currently unused by the backend, kept for reference/future dashboard-side use) |
| `SUPABASE_SERVICE_ROLE_KEY` | Secret key — bypasses RLS. This is what the bot and dashboard actually use to read/write |

## Bot commands

| Command | Does |
|---|---|
| *(send a photo, voice note, or text)* | Logs a transaction (or, if it's a question or correction, routes there instead — see below) |
| *(send a statement PDF or photo/album)* | Extracts miles earned, balance, and expiry data per card |
| `/budget` | Set or redefine this month's budget |
| `/recurringexpense` | Manually register a recurring charge |
| `/safetospend` | On-demand safe-to-spend figure |
| `/insights` | Trigger the weekly digest on demand |
| *(ask a question in plain English)* | e.g. "how much did I spend on Grab last month" |
| *(state a correction in plain English)* | e.g. "actually that Starbucks was $6 not $5" |

## Design notes

- **Single-user by design.** No `user_id` columns anywhere — the bot is locked to
  one Telegram account via `TELEGRAM_ALLOWED_USER_ID`, so a column that's always the
  same value would add nothing.
- **Display vs. storage dates.** Everything is stored and passed between functions
  as ISO (`YYYY-MM-DD`) — the only place dates get reformatted to `DD/MM/YYYY` is
  right before being shown to the user in a Telegram message or the dashboard
  (`bot/dates.py`).
- **Determinism over creativity.** Every structured-extraction and classification
  call uses `temperature=0` — this is a data-accuracy tool, not a creative one, and
  reducing run-to-run variance measurably reduced misreads during testing.
- **Fuzzy matching, used deliberately.** Merchant names (for recurring-charge
  detection) and bank names (for grouping multi-statement photo albums) are
  normalized and matched by substring containment, not exact string equality —
  otherwise "Citi" and "Citibank" on different pages of the same statement would
  incorrectly split into two separate statements.
- **Confirm before writing, always.** Every write triggered by natural language
  (corrections) or ambiguous matching (unrecognized card on a statement) shows the
  user what's about to happen and waits for an explicit button tap — nothing gets
  silently mutated.
- **Estimates are labeled as estimates.** The miles-expiry timeline is explicitly
  flagged as a FIFO-based projection, not a real balance, everywhere it's shown —
  this system can't see redemptions made outside itself.

## Known limitations / things to revisit

- **Per-bank statement tuning**: extraction prompts were built and verified against
  real DBS and Citi statements. HSBC (or any other bank/format) hasn't been tested —
  the extraction logic is written generically, not hardcoded to one bank's layout,
  but accuracy on an untested format isn't guaranteed until checked against a real
  example.
- **`card_last4` seeding**: only DBS cards have `card_last4` pre-seeded in
  `db/schema.sql`. Other cards get matched via the bot's "which card is this?"
  fallback the first time you upload that card's statement — there's no auto-save
  of that mapping back to `payment_methods` yet, so it'll ask again on every
  statement until `card_last4` is set manually in Supabase.
- **Album debounce**: multi-photo Telegram sends are batched with a 5-second
  debounce (resets per photo, not a fixed total wait) to make sure slow-arriving
  photos aren't dropped from the batch. Very large or slow uploads could still
  theoretically exceed this.
- **Recurring-charge frequency projection**: only `monthly`/`quarterly`/
  `semi_annual`/`yearly` charges are projected into safe-to-spend's "upcoming
  recurring charges" figure — `custom`-frequency charges are tracked but not
  automatically projected, since there's no structured interval to compute from.
