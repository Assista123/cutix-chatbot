# Cutix demo chatbot v0.4 — lead-generation restructure

You need: a laptop, internet, and your Gemini key (free tier is fine for demo). No coding.
Never share your key, the .env file, or a screenshot showing the key.

## What changed in v0.4

- **Primary job** is now lead qualification (gentle), not pure answering.
- System prompt v0.4: answer first, qualify only on clear buying intent.
- SQLite (WAL) primary logging with qualification stage, lead fields, and routing outcome. JSONL still written as archival.
- Server-side state machine: answering → intent_detected → qualifying → lead_captured → routed.
- When a lead is captured, the WhatsApp button is updated with a pre-filled summary.
- History window default trimmed to 4 messages (configurable via `HISTORY_WINDOW`).
- Simple internal stats at `/stats`.
- Groq / Llama 3.1 8B stack is prepared (via the existing openai-compatible path) but deferred until paid plan. Demo stays on Gemini free tier.

## One-time setup (about 10 minutes)

1. **Install Python.** Go to python.org, download Python 3.10 or newer, run it, and tick "Add python.exe to PATH" on the first screen.
2. **Unzip** this folder somewhere easy, for example the Desktop.
3. **Make your settings file.** In the folder, copy `.env.example` and rename the copy to `.env` (exactly, with no ".example"). Open it in Notepad.
   - Change `PROVIDER=mock` to `PROVIDER=gemini`
   - Paste your key after `GEMINI_API_KEY=` (no spaces, no quotes)
   - Save.
4. **Open a Command Prompt in the folder.** Click the folder's address bar, type `cmd`, press Enter.
5. **Check your key and model name.** Type: `python list_models.py` and press Enter. It prints the models your key can use. Find the Gemini 3.1 Flash Lite entry and copy its exact name into `GEMINI_MODEL=` in `.env` if it differs from what is there. Save.
6. **Start the bot.** Type `python server.py` (or double-click `start.bat`). Open http://localhost:8000 in your browser.

On Mac or Linux, use `python3` instead of `python`.

## Check it works

- Ask: "What does Cutix make?" You should get a real answer, not "[Demo mode...]".
- Ask something with buying intent, e.g. "I need 500m of 2.5mm house wiring for my shop in Port Harcourt". The bot should answer then gently collect missing details and hand off. After a qualified lead the WhatsApp button becomes "Continue on WhatsApp" with a pre-filled summary.
- Stats (for you, not public): http://localhost:8000/stats
- If you get "Sorry, I can't answer that right now", look at the black window. It prints the reason, for example a wrong key or model name.
  - Reply cut off or empty: add this line to `.env`:  `GEMINI_EXTRA_JSON={"thinkingConfig":{"thinkingBudget":0}}`  and restart. If it then gives an error, delete that line.
  - "429": you hit the free limit. Wait a minute.

## Run the 50 test questions

In the Command Prompt: `python run_tests.py --showpieces` (6 key questions), then `python run_tests.py` for all 50 (about 6 minutes, paced to stay inside the free limit). It writes a file called `test_results_...md`. Open it, read each answer against "Expected", and mark Pass or Fail. Lines marked "Auto flags" need a close look: they catch invented phone numbers and emails, prices, and over-long answers.

**Still needed for V2 validation (noted):**
- Re-run the 50-question accuracy set on the current model.
- Add / run a qualification-flow test set (soft intent, clear buying intent, incomplete vs complete lead, multi-turn).
- 100 measured calls for real token counts (once on paid / Groq).

Send me that file (not your .env) and I will fix what fails.

## Show it on your phone

1. In `.env` set `HOST=0.0.0.0` and restart the bot.
2. Connect your phone to the same Wi-Fi as the laptop.
3. Find the laptop's address: in Command Prompt type `ipconfig` and note the "IPv4 Address", for example 192.168.1.20.
4. On the phone open http://192.168.1.20:8000
(If it does not open, Windows Firewall may be blocking it. Allow Python when Windows asks.)

For the screen recording, use your phone's screen recorder while you ask the showpiece questions.

## What it does and does not do

- Answers only from the files in the `data` folder. To update facts, replace those files and restart.
- Never sends your key to the browser. The key stays in `.env` on your laptop.
- Limits: 20 messages per visitor, 300 per day in total, and 12 requests per minute to the model. Change them in `.env`.
- Saves each question and answer in SQLite (`logs/chats.db`) and JSONL (`logs/chats.jsonl`). The chat page tells visitors that conversations may be reviewed.
- Tracks qualification stage and lead fields (name, need, quantity, location) per session.
- The chat page footer shows a demo contact email (set `CONTACT_EMAIL` in `.env`).
- Optional access code: set `ACCESS_CODE=` in `.env` and visitors must enter it.

## Later: putting it online + paid model

The same folder can run on a small VPS (Hetzner CX22 recommended). That is a separate step, and we will do it once the demo is approved. Before real customers use it, switch to a paid model plan (Groq Llama 3.1 8B Instant is the planned primary). Free tiers may use chats to improve the provider's products.

## Files

- `server.py` the bot (v0.4 lead-gen); `static/index.html` the chat page; `data/` the knowledge it answers from
- `run_tests.py` tests; `list_models.py` shows model names
- `logs/chats.db` SQLite (WAL); `logs/chats.jsonl` archival
