# Nelly

Nelly is a personal expense-tracking Telegram bot.
You send her a message like *"lunch 25"* and she logs it in Supabase.
You ask her *"summary this week"* and she replies with a breakdown.

Under the hood she's a Claude agent (via the [Claude Agent SDK](https://docs.anthropic.com/en/docs/agent-sdk)) with two tools that read and write a Postgres table in Supabase. The whole thing is three Python files.

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
                            ┌──────────────────┼──────────────────┐
                            ▼                  ▼                  ▼
                       ┌─────────┐      ┌────────────┐     ┌────────────┐
                       │Telegram │      │   Claude   │     │  Supabase  │
                       │   API   │      │  (Sonnet)  │     │ (Postgres) │
                       └─────────┘      └────────────┘     └────────────┘
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
The brain. Two MCP tools via `@tool`:

- **`add_expense(date, description, amount, category, currency)`** — writes a row.
- **`list_expenses(start_date, end_date)`** — returns rows so Claude can summarize them.

System prompt tells Claude who she is, today's date, the category vocabulary, and to reply briefly in the user's language.

### `db.py` — Supabase wrapper
A thin HTTP layer over Supabase's [PostgREST](https://supabase.com/docs/guides/api). Uses the service-role key (RLS bypass — fine for a single-user app).

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
| `created_at` | timestamptz | auto |

To extend toward real Splitwise (multiple participants per expense), add an `expense_splits` table referencing `expenses.id`. Not needed yet.

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
| `TELEGRAM_ALLOWED_USER_ID` | Only this Telegram user can use the bot |

### Run locally (without EC2)
```bash
python main.py
```

Then DM your bot. Try:
- `lunch 25`
- `uber to SFO 32 yesterday`
- `summary this week`
- `how much did I spend on food in May?`
