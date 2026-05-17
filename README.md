# Nelly

Nelly is a personal expense-tracking Telegram bot.
You send her a message like *"lunch 25"* and she logs it in Supabase.
You ask her *"summary this week"* and she replies with a breakdown.

Under the hood she's a Claude agent (via the [Claude Agent SDK](https://docs.anthropic.com/en/docs/agent-sdk)) with four tools that read and write a Postgres table in Supabase, and on demand mirror the table into a Google Sheet. The whole thing is four Python files.

---

## Architecture

```
        you edit
   ┌───────────────┐    git push    ┌──────────┐
   │     Laptop    │ ─────────────► │  GitHub  │
   └───────────────┘                └────┬─────┘
            │                            │ git pull
            │  ./deploy.sh               ▼
            └──────────────────────► ┌──────────────────────┐
                                     │   EC2 (us-east-1)    │
                                     │   systemd → main.py  │
                                     └─────────┬────────────┘
                                               │
                  ┌────────────┬────────────┼────────────┬────────────┐
                  ▼            ▼            ▼            ▼            ▼
            ┌─────────┐  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌───────────┐
            │Telegram │  │  Claude  │ │ Supabase │ │  Google  │ │  service  │
            │   API   │  │ (Sonnet) │ │(Postgres)│ │  Sheets  │ │  account  │
            └─────────┘  └──────────┘ └──────────┘ └──────────┘ └───────────┘
```

**GitHub is the source of truth.** What runs on EC2 = what's on `main`. To deploy you `git push` + run `./deploy.sh`. The instance never has uncommitted code.

### Runtime flow

1. **You** send a Telegram message.
2. **`main.py`** receives it, checks you're the allowed user, hands the raw text to the agent.
3. **`agent.py`** spins up a Claude agent. Claude reads your message, decides whether it's an *expense to log* or a *question to answer*, and calls one of two tools.
4. Those tools live in **`db.py`**, which speaks to Supabase over its REST API.
5. Claude writes a short reply. `main.py` sends it back to Telegram.

Each message is one independent agent turn — no chat memory. Keeps the code dead simple. If you ever want multi-turn ("yes, save it", "no, change category to taxi"), reuse the `ClaudeSDKClient` across messages.

---

## The files

### `main.py` — Telegram entrypoint
Boots a Telegram bot with [`python-telegram-bot`](https://python-telegram-bot.org), polls for new messages, and routes them.

- **Whitelist:** only messages from `TELEGRAM_ALLOWED_USER_ID` are processed.
- **First-run helper:** if the env var is empty, the bot replies with the sender's Telegram user ID.

### `agent.py` — the Claude agent and its tools
The brain. Four MCP tools via `@tool`:

- **`add_expense(date, description, amount, category, currency, tag)`** — writes a row.
- **`list_expenses(start_date, end_date)`** — returns rows for summaries and to look up ids.
- **`update_expense(id, …fields)`** — PATCHes an existing row. Empty string = leave field alone.
- **`sync_sheet()`** — overwrites the Google Sheet mirror with all expenses. Called when you ask Nelly to *sync*, *export*, or *refresh the sheet*.

System prompt tells Claude who she is, today's date, the category vocabulary, when to tag, when to clarify on edits, and to reply briefly in the user's language.

### `db.py` — Supabase wrapper
A thin HTTP layer over Supabase's [PostgREST](https://supabase.com/docs/guides/api). Uses the service-role key (RLS bypass — fine for a single-user app). Exposes `insert_expense`, `fetch_expenses` (date range), `fetch_all_expenses` (everything, for the sheet sync), and `update_expense`.

### `sheets.py` — Google Sheet mirror
A tiny wrapper around [`gspread`](https://docs.gspread.org). `sync_to_sheet(rows)` clears the target tab and writes `[headers] + rows`. Column order is pinned in code so a Supabase schema drift can't silently scramble the sheet. Auth is a service-account JSON in `creds/service-account.json` — that file is gitignored and lives outside the repo's tracked files.

### `deploy.sh` — push to prod
Refuses to run with uncommitted changes, `git push`es, SSHes into EC2, `git pull`s, reinstalls deps if `requirements.txt` changed, restarts systemd, and prints the last few log lines.

### `requirements.txt` / `.env`
Pinned deps; secrets. `.env` is gitignored — secrets exist twice: once on your laptop, once on the EC2 box.

---

## The database

Single table `expenses`:

| column | type | notes |
|--------|------|-------|
| `id` | uuid | auto |
| `date` | date | when the expense happened |
| `description` | text | |
| `category` | text | Dining out / Taxi / Groceries / Liquor / … |
| `amount` | numeric(10,2) | |
| `currency` | text | default `USD` |
| `paid_by` | text | reserved for Splitwise-style sharing |
| `notes` | text | unused |
| `tag` | text | optional free-form label, only set when the user asks |
| `created_at` | timestamptz | auto |

To extend toward real Splitwise (multiple participants per expense), add an `expense_splits` table referencing `expenses.id`. Not needed yet.

### `despensa_stock` — pantry inventory snapshots

Each fridge photo Nelly reads becomes a dated snapshot. "Current stock" = the rows
with the most recent `captured_on`; re-sending a photo the same day replaces that
day's rows. Item names are stored in Spanish.

| column | type | notes |
|--------|------|-------|
| `id` | uuid | auto |
| `captured_on` | date | the day of the photo / assessment |
| `item` | text | Spanish, e.g. `huevos`, `leche` |
| `quantity` | text | free text: `6`, `1 cartón`, `medio paquete`, `poco` |
| `category` | text | Spanish pantry category, optional |
| `created_at` | timestamptz | auto |

### `weekly_menu` — the planned menu

One row per `(menu_date, meal, eater)` — `meal` is `desayuno`, `almuerzo`, or
`cena`; `eater` is `adulto` or `bebé` (there's an infant in the household who
eats different, age-appropriate food). The `unique (menu_date, meal, eater)`
constraint means re-planning a slot upserts in place, and the same date+meal
can hold one adult and one baby dish. Dishes are stored in Spanish. The grocery
list is **not** stored — Nelly builds it on demand from `weekly_menu` (both
eaters) minus the latest `despensa_stock`.

| column | type | notes |
|--------|------|-------|
| `id` | uuid | auto |
| `menu_date` | date | the day this dish is for |
| `meal` | text | `desayuno` / `almuerzo` / `cena` |
| `eater` | text | `adulto` (default) or `bebé` |
| `dish` | text | Spanish, e.g. `pasta con tomate` |
| `notes` | text | optional |
| `created_at` | timestamptz | auto |

Schema (already created in Supabase):

```sql
create table despensa_stock (
  id          uuid primary key default gen_random_uuid(),
  captured_on date not null,
  item        text not null,
  quantity    text,
  category    text,
  created_at  timestamptz default now()
);
create index despensa_stock_captured_on_idx on despensa_stock (captured_on desc);

create table weekly_menu (
  id         uuid primary key default gen_random_uuid(),
  menu_date  date not null,
  meal       text not null,
  eater      text not null default 'adulto',  -- 'adulto' | 'bebé'
  dish       text not null,
  notes      text,
  created_at timestamptz default now(),
  unique (menu_date, meal, eater)
);
create index weekly_menu_menu_date_idx on weekly_menu (menu_date);
```

If `weekly_menu` was created before the `eater` column existed, migrate with:

```sql
alter table weekly_menu add column if not exists eater text not null default 'adulto';

do $$
declare c text;
begin
  select conname into c from pg_constraint
   where conrelid = 'weekly_menu'::regclass and contype = 'u';
  if c is not null then execute format('alter table weekly_menu drop constraint %I', c); end if;
end $$;

alter table weekly_menu add constraint weekly_menu_menu_date_meal_eater_key
  unique (menu_date, meal, eater);
```

---

## Where it runs

- **GitHub:** [crichardll/nelly](https://github.com/crichardll/nelly) — source of truth.
- **EC2:** `t4g.micro` in `us-east-1` (free tier). Public IP `100.30.215.66`. Systemd unit `nelly.service` keeps the bot alive across crashes and reboots.

SSH in:
```bash
ssh -i ~/.ssh/nelly-bot.pem ubuntu@100.30.215.66
```

Useful commands on the box:
```bash
sudo systemctl status nelly        # is it running?
sudo journalctl -u nelly -f        # tail logs
sudo systemctl restart nelly       # restart (e.g. after manual .env edit)
```

---

## Day-to-day workflow

```bash
# edit something
vim agent.py

# commit
git add agent.py
git commit -m "tweak prompt"

# deploy
./deploy.sh
```

That's it.

---

## First-time setup (already done — for reference)

### Locally
```bash
pip install -r requirements.txt
cp .env.example .env  # fill in the values
```

`.env` keys:

| Variable | What it is |
|----------|-----------|
| `SUPABASE_URL` | Project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | DB write key — never expose |
| `ANTHROPIC_API_KEY` | For the Claude agent |
| `TELEGRAM_BOT_TOKEN` | From [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_ALLOWED_USER_ID` | Comma-separated Telegram user IDs allowed to use the bot (e.g. `111,222`) |
| `GOOGLE_SHEET_ID` | ID of the sheet to mirror into |
| `GOOGLE_SHEET_TAB` | Tab name (default `BBDD_Gastos`) |
| `GOOGLE_CREDENTIALS_FILE` | Path to the service-account JSON key |

### Run locally (without EC2)
```bash
python main.py
```

Then DM your bot. Try:
- `lunch 25`
- `uber to SFO 32 yesterday`
- `summary this week`
- `how much did I spend on food in May?`
- `the trader joe was 240 not 246` *(edit)*
- `sync the sheet` *(mirror Supabase → Google Sheet)*

---

## Sheet sync — one-time setup

The `sync_sheet` tool mirrors the Supabase `expenses` table into the `BBDD_Gastos` tab of a Google Sheet. **Each sync clears the tab and rewrites everything** from Supabase — the sheet is a read-only view of the DB.

Setup is done once per machine:

1. [console.cloud.google.com](https://console.cloud.google.com) → create a project (or reuse one).
2. **APIs & Services → Library** → enable both **Google Sheets API** and **Google Drive API**.
3. **IAM & Admin → Service Accounts** → create one named `nelly-sheets`. Skip role assignment.
4. Open the service account → **Keys → Add key → JSON** → download.
5. `mkdir -p creds && mv ~/Downloads/<key>.json creds/service-account.json`. The `creds/` directory is gitignored.
6. Copy the `client_email` field out of the JSON (looks like `nelly-sheets@<project>.iam.gserviceaccount.com`).
7. Open your target sheet → **Share** → paste that email → **Editor** → uncheck "Notify".
8. Add the three `GOOGLE_*` vars to `.env`.
9. `pip install -r requirements.txt` (pulls in `gspread`).
10. Smoke test: `python -c "import sheets; print(sheets.service_account_email())"` should print the same email.

After this, just message Nelly with `sync sheet` to refresh the mirror. If you ever forget to share the sheet with the service account, the error message will tell you which email to share with.
