# Meeting Copilot

**A free, always-on-top helper for Zoom, Google Meet, Teams, or any video call on Windows.** It shows a live transcript of the other person and, when they ask you something, writes an answer you can say out loud, built from **your own Obsidian notes** and what's **on your screen**.

- **Runs only on your PC.** Nothing joins the call, and no audio is saved: audio is held in memory for about a second while it's transcribed, then thrown away.
- **Grounded in your notes.** Every fact in an answer has to come from your vault, and an automatic check rewrites anything that isn't backed by a note.
- **Free.** It uses free API keys from Groq, with NVIDIA Build as an optional free backup.
- **Hidden from screen share,** so the panel doesn't show up when you share your screen.

> **Use it responsibly.** Some interviews, exams and classes don't allow outside help or AI tools. Check the rules before you use it, and tell people if you save call transcripts where that's required.

---

## What you need

| | |
|---|---|
| **Windows 10 or 11** | It listens to your PC's sound output through Windows' WASAPI loopback |
| **Python 3.12** | From [python.org](https://www.python.org/downloads/). Tick **"Add python.exe to PATH"** when you install |
| **A free Groq API key** | From [console.groq.com/keys](https://console.groq.com/keys). Required, for transcription and answers |
| **A free NVIDIA Build key** | From [build.nvidia.com](https://build.nvidia.com), then **Get API Key**. Optional but recommended as a backup, and for reading your screen |
| **An Obsidian vault** | Optional but recommended: notes about you (activities, goals, stories, prep notes). Any folder of `.md` files works |

---

## Setup (about 10 minutes)

1. **Download** this repo: click the green **Code** button, then **Download ZIP**, and unzip it somewhere you'll keep it (for example `Documents\MeetingCopilot`).
2. **Double-click `setup.bat`.** It creates a private Python environment and installs everything. When Notepad opens `.env`, paste your keys and save:
   ```
   GROQ_API_KEY=gsk_...
   NVIDIA_API_KEY=nvapi-...    (optional)
   ```
3. **Open `config.json`** and set two things:
   ```json
   "your_name": "Alex",
   "vault_path": "~/Documents/MyVault",
   ```
   `vault_path` is the folder of your Obsidian vault. `~` means your user folder, such as `C:\Users\you`.
4. **Put notes about yourself in a `00 Me` folder** in your vault (or list exact files in `pinned_notes`, see [Configuration](#configuration)). Those notes go into every answer.
5. **Double-click `start.bat`.** The panel appears on the right side of your screen. If something goes wrong, use `start-debug.bat`, which keeps a console window open with live log messages.
6. *(Optional)* Double-click **`Create Shortcut.vbs`** to add Meeting Copilot to the Start menu, then right-click it there and choose **Pin to taskbar**.

> Windows SmartScreen may warn you about the `.bat` or `.vbs` files because they're downloaded scripts. Open them in Notepad first to see what they do, then choose **More info → Run anyway**.

---

## Using it

### 1. Set the topic

When the panel opens it asks **"What's this call about?"** Type something like *State U alumni interview*, *Job interview at Acme*, or *Debate practice: social media should be regulated* and press Enter. Press Esc to skip.

- **Interviews** (college, job, scholarship, coach): it answers as you, about you, and pulls notes that match the school or company first.
- **Debates, classes, discussions:** it answers the topic itself, with a clear position, 2–3 points or a direct rebuttal.
- The topic also helps the transcriber spell names correctly.
- Click the title in the panel header, or press `Ctrl+Alt+T`, to change the topic mid-call.

### 2. Join your call as usual

The other person's words appear in yellow under **Live transcript**. When they ask a question, an answer streams into the top box: two short paragraphs, about 70–110 words, written to sound like a real person talking.

### Hotkeys

| Keys | Does |
|---|---|
| `Ctrl+Alt+A` | Answer now (if auto missed a question) |
| `Ctrl+Alt+M` | **More:** a longer version of the last answer |
| `Ctrl+Alt+B` | **Bigger:** switch between a tall panel and your normal size |
| `Ctrl+Alt+P` | Turn auto-answer on or off |
| `Ctrl+Alt+S` | Re-read the screen now (for example, after they share slides) |
| `Ctrl+Alt+T` | Change the call topic |
| `Ctrl+Alt+L` | Pause or resume saving the transcript |
| `Ctrl+Alt+H` | Hide or show the panel |
| `Ctrl+Alt+Q` | Quit |

Drag the panel by its title and resize it from the corner. Its size and position are remembered.

---

## Features

- **Fact check (free, automatic).** Your notes become numbered fact lines that keep their subject, for example *People: Sam, close friend*. The model tags each sentence with the facts it used, and the app checks the numbers, names and achievement words (won, captain, state, internship…) against them. Made-up details get rewritten from the right facts. If a rewrite can't happen because of a rate limit, the sentence is underlined in orange.
- **Memory.** It remembers what it already suggested in this call and refers back ("like I mentioned earlier…") instead of repeating itself. It also reads your past saved calls so your stories stay consistent.
- **Solve mode.** Math, logic and general-knowledge questions ("what's the derivative of ln(x+2)?") get a carefully reasoned answer first, then a short spoken explanation.
- **Screen reading.** Every few seconds it checks whether your screen changed and summarizes what's visible (shared slides, chat, names). Turn this off with `"screen_watch": false`.
- **Saved sessions.** Each call is saved as a Markdown note in your vault (`Journal/Calls/` by default), written line by line so nothing is lost if the app closes. Saved calls are never used as facts about you. Audio is never saved, only text.

---

## Configuration

Settings live in `config.json`. Anything you leave out uses the default from the top of `copilot.py`.

| Setting | Default | What it does |
|---|---|---|
| `your_name` | `"Me"` | Your first name, as the AI should write it |
| `vault_path` | `"~/Documents/MyVault"` | Your Obsidian vault folder |
| `pinned_notes` | `[]` | Notes sent whole with **every** answer, such as `["00 Me/About Me.md", "00 Me/Goals.md"]` |
| `pinned_folders` | `["00 Me"]` | Used when `pinned_notes` is empty: every note in these folders is pinned |
| `folder_boosts` | `{"Essays": 1.5, …}` | Give folders more (>1) or less (<1) weight in search |
| `exclude_folders` / `exclude_notes` | | Folders or notes that should never be used as facts |
| `session_folder` | `"Journal/Calls"` | Where call transcripts are saved. Keep the last part named `Calls` |
| `save_sessions` | `true` | Save each call as a note |
| `ask_topic_on_start` | `true` | Ask "What's this call about?" on launch |
| `auto_answer` | `true` | Answer automatically when a question is detected |
| `listen_to_mic` | `false` | Also transcribe **you** (use headphones, or your mic picks up the other person too) |
| `transcriber` | `"groq"` | `"local"` runs offline with no rate limits (needs `pip install faster-whisper`, about 1 s slower) |
| `answer_reasoning` | `"low"` | `"medium"` or `"high"` gives more careful but slower answers |
| `screen_watch` | `true` | Read your screen for context |
| `silence_ms` | `450` | Pause length that ends a sentence. Lower responds faster but may cut in mid-pause |
| `hide_overlay_from_capture` | `true` | Keep the panel out of screenshots and screen share |

---

## Free limits and speed

- **Groq's free tier** allows about 20 transcriptions a minute and has per-minute token limits. Adding an **NVIDIA Build** key gives you an automatic backup that rarely runs out during a call.
- **Speed:** about 1–1.5 seconds from when they stop talking to the first words of the answer. Backup answers from NVIDIA start a bit slower.
- **Privacy:** transcribed text and screen summaries are sent to Groq (and NVIDIA, if you add a key) to generate answers. NVIDIA's free access is meant for testing, and prompts may be used to improve its models. Turn off `screen_watch` if you don't want screenshots sent anywhere.

---

## Troubleshooting

- **"Python not found."** Install Python 3.12 from python.org with **Add to PATH** ticked, then run `setup.bat` again.
- **"Missing GROQ_API_KEY."** Open `.env` next to `copilot.py` and paste your key after `GROQ_API_KEY=` with no spaces.
- **No transcript appears.** The call audio has to play through your default Windows output device. Check **Settings → System → Sound → Output**.
- **"Vault not found."** Fix `vault_path` in `config.json`.
- **Answers say "(no notes)".** Nothing in your vault matched the question, so that line is a general suggestion. Add notes about the topic.
- **Rate-limit errors.** Add an NVIDIA key, or switch to `"transcriber": "local"`.
- **Anything else:** run `start-debug.bat` and read `copilot.log`, which records timings and errors only, never what anyone said.

---

## Files

| File | What it is |
|---|---|
| `copilot.py` | The whole app |
| `config.json` | Your settings |
| `.env.example` | Template for your API keys. `setup.bat` copies it to `.env`, which is never committed |
| `requirements.txt` | Python packages |
| `setup.bat` / `start.bat` / `start-debug.bat` | Install, run, and run with a log window |
| `Create Shortcut.vbs` | Adds a Start menu shortcut you can pin |
| `copilot.ico` | App icon |

## License

[MIT](LICENSE)
