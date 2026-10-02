# MafiaBot

Discord Mafia game bot.

## Folder layout

- `bot.py`: entrypoint
- `cogs/`: cogs (extensions)
- `app.py`, `game.py`, `config.py`, etc.: support modules
- `util.py`: small helpers (including `.env` loading)

## Run (PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Option A: .env file (recommended)
# Create a file named ".env" in this folder:
# DISCORD_BOT_TOKEN=your_token_here

# Option B: set it in this PowerShell session
# $env:DISCORD_BOT_TOKEN="your_token_here"

python bot.py
```



