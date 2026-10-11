# GPA-Calculator-Bot

Telegram bot that answers grade questions written in plain language. Gemini API reads the message and pulls out the numbers into a structured request (Pydantic schema); the math itself is done in Python.

## What it calculates
- **Course grade**: the score needed on the final exam to reach a target grade, based on weighted components (assignments, quizzes, midterm).
- **CGPA**: the new cumulative CGPA after a semester, or the semester GPA needed to reach a target CGPA.

Example message:
> Current CGPA 3.64, 90 credits done, next semester is 20 credits. What do I need to get 3.8?

## Stack
Python, pyTelegramBotAPI, Google Gemini API (`google-genai`, structured output), Pydantic

## Run
```bash
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN="your-telegram-bot-token"
export GEMINI_API_KEY="your-gemini-api-key"
python3 bot.py
```
