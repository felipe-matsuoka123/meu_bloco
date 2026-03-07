# Simple Notes App

A small Python notes app with:

- a login page
- a registration page
- SQLite note storage
- hashed passwords
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

3. Set a secret key:

   ```bash
   export SECRET_KEY="replace-this-with-a-random-secret"
   export GEMINI_API_KEY="your-google-gemini-api-key"
   ```

   `SECRET_KEY` is just a long random private string used by Flask to protect sessions and login cookies.
   Example:

   ```bash
   export SECRET_KEY="8f1c6c6e7f194d0c8f2dbd3e7a0a9c3142e6b1baf4f54b2b"
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

## One-command startup

You can also edit the variables at the top of [start_app.sh](/home/felipe/projects/meu_bloco/start_app.sh) and run:

```bash
chmod +x start_app.sh
./start_app.sh
```

This will:

- create `.venv` if needed
- install requirements
- export your keys
- start the Flask app

## Notes

- Notes are stored in `notes.db`.
- Create users in the `/register` page.
- Passwords are stored hashed, not in plain text.
- Notes belong to the logged-in user only.
- The review tool lives in the notes sidebar and only uses the notes you select as context.
- Each note can be exported locally as a PDF.
- The default Gemini model is `gemini-2.5-flash-lite` and can be changed with `GEMINI_MODEL`.
- If you already had an old `notes.db`, the app migrates old notes to a fallback `legacy` user.
- For any public exposure, set a strong `SECRET_KEY` first.
