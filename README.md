# Telegram Reminder Bot

A Telegram bot that accepts natural language reminders and sends them back at the scheduled time. Supports forum topics — reminders are delivered to the same topic they were created in.

## Setup

### 1. Create a Telegram Bot
1. Open Telegram and message [@BotFather](https://t.me/BotFather)
2. Send `/newbot` and follow the prompts
3. Copy the bot token

### 2. Configure
```bash
cp .env.example .env
# Edit .env and paste your bot token
```

### 3. Run Locally
```bash
pip install -r requirements.txt
python bot.py
```

## Usage

Send any message to the bot with a time reference:

- `remind me to buy milk in 2 hours`
- `call mom tomorrow at 3pm`
- `meeting in 30 minutes`

### Commands
- `/start` — show help
- `/list` — see upcoming reminders
- `/cancel <id>` — cancel a reminder

## Deploy to a Server

### Option A: Railway
1. Push this code to a GitHub repo
2. Go to [railway.app](https://railway.app), create a new project from the repo
3. Add environment variable `BOT_TOKEN` in the Railway dashboard
4. Deploy — Railway will use the Dockerfile automatically

### Option B: Render
1. Push to GitHub
2. Go to [render.com](https://render.com), create a **Background Worker**
3. Set the build command to `pip install -r requirements.txt`
4. Set the start command to `python bot.py`
5. Add `BOT_TOKEN` as an environment variable

### Option C: VPS (Ubuntu)
```bash
# Clone your repo
git clone <your-repo-url> ~/reminder-bot
cd ~/reminder-bot

# Install dependencies
pip install -r requirements.txt

# Create .env with your token
cp .env.example .env
nano .env

# Run with Docker
docker build -t reminder-bot .
docker run -d --name reminder-bot --restart unless-stopped -e BOT_TOKEN=your-token reminder-bot

# Or run with systemd (create /etc/systemd/system/reminder-bot.service):
# [Unit]
# Description=Telegram Reminder Bot
# After=network.target
#
# [Service]
# WorkingDirectory=/root/reminder-bot
# ExecStart=/usr/bin/python3 bot.py
# EnvironmentFile=/root/reminder-bot/.env
# Restart=always
#
# [Install]
# WantedBy=multi-user.target
#
# Then: systemctl enable --now reminder-bot
```
