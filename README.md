# Simple Notes App

A small Python notes app with:

- a login page
- a registration page
- PostgreSQL note storage
- bcrypt-hashed passwords
- session-based authentication
- an AI review tool for selected notes
- explicit note selection for AI context
- PDF export for individual notes
- a simple UI that works well through `ngrok`

## Run locally

1. Create a virtual environment:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Set your environment:

   ```bash
   export SECRET_KEY="replace-this-with-a-random-secret"
   export DATABASE_URL="postgresql://postgres:postgres@localhost:5432/meu_bloco"
   export GEMINI_API_KEY="your-google-gemini-api-key"
   export STRIPE_SECRET_KEY="sk_test_..."
   export STRIPE_PRICE_LOOKUP_KEY="starter_plan"
   export GIFT_CARD_OVERRIDE_CODE="TEST-GIFT"
   ```

   `SECRET_KEY` is a long random private string used by Flask to protect sessions and login cookies.
   `DATABASE_URL` must point at an existing PostgreSQL database.
   Example:

   ```bash
   export SECRET_KEY="8f1c6c6e7f194d0c8f2dbd3e7a0a9c3142e6b1baf4f54b2b"
   export DATABASE_URL="postgresql://postgres:postgres@localhost:5432/meu_bloco"
   ```

4. Start the app:

   ```bash
   python app.py
   ```

5. Open:

   ```text
   http://127.0.0.1:5000
   ```

## Expose it with ngrok

With the app running on port `5000`:

```bash
ngrok http 5000
```

Use the HTTPS forwarding URL from `ngrok` to access the app remotely.

## Project layout

- [app.py](/home/felipe/projects/meu_bloco/app.py): Flask routes and application logic
- [db.py](/home/felipe/projects/meu_bloco/db.py): PostgreSQL connection and query helpers
- [schema.sql](/home/felipe/projects/meu_bloco/schema.sql): Postgres schema
- [Procfile](/home/felipe/projects/meu_bloco/Procfile): production start command for Railway

## Railway

1. Add a PostgreSQL service in Railway.
2. Deploy this repo as an app service in the same Railway project.
3. Set these variables on the app service:
   - `SECRET_KEY`
   - `GEMINI_API_KEY`
   - `STRIPE_SECRET_KEY`
   - `STRIPE_PRICE_LOOKUP_KEY`
   - `GIFT_CARD_OVERRIDE_CODE`
4. Make sure `DATABASE_URL` is available to the app service from the PostgreSQL service.
5. Railway can use [Procfile](/home/felipe/projects/meu_bloco/Procfile) or this explicit start command:

   ```bash
   gunicorn app:app --bind 0.0.0.0:$PORT
   ```

6. Optional health check path:

   ```text
   /healthz
   ```

## Notes

- Notes are stored in PostgreSQL.
- Create users in the `/register` page.
- Passwords are stored hashed, not in plain text.
- New passwords use `bcrypt`.
- After 5 failed login attempts, the account is locked for 15 minutes.
- The `/pricing` page starts a Stripe Checkout flow when `STRIPE_SECRET_KEY` and `STRIPE_PRICE_LOOKUP_KEY` are configured.
- The registration form accepts an optional `GIFT_CARD_OVERRIDE_CODE` that bypasses billing for testing.
- Notes belong to the logged-in user only.
- The review tool lives in the notes sidebar and only uses the notes you select as context.
- Each note can be exported locally as a PDF.
- The default Gemini model is `gemini-3-flash-preview` and can be changed with `GEMINI_MODEL`.
- Gemini requests time out after 20 seconds by default; override with `GEMINI_TIMEOUT_SECONDS`.
- Gunicorn uses a 45-second worker timeout by default in the `Procfile`; override with `GUNICORN_TIMEOUT`.
- PostgreSQL tables are created automatically on startup from [schema.sql](/home/felipe/projects/meu_bloco/schema.sql).
- For any public exposure, set a strong `SECRET_KEY` first.
