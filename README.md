# BookMarkCore

A personal Python bot that turns Telegram messages into a browsable HTML bookmark catalog. It classifies text and links using hosted language models and publishes new cards via the GitHub API.

[Read the source](main.py) · [Catalog interface](index.html) · [Русский](README.ru.md)

## What the code does

1. Receives a message and extracts the link when present.
2. Requests a category from an LLM API, parses the JSON response and tries fallback models or a rule-based classification when needed.
3. Checks the existing HTML for a matching normalized URL or title before publishing. The normal link flow lets the user cancel or confirm a possible duplicate.
4. Inserts the card into the category section and commits the change to the configured GitHub repository.

The bot is built with Python, aiogram, asyncio, PyGithub, Hugging Face Inference and aiohttp. The repository contains the implementation and catalog interface. The model calls use existing models.

## Setup in a test repository

Create a virtual environment, activate it and install the packages from [requirements.txt](requirements.txt).

```sh
python -m pip install -r requirements.txt
```

Set these variables locally in `.env`. Use your numeric Telegram ID as `ADMIN_ID`. A value of `0` disables the owner check.

```dotenv
TG_TOKEN=
GITHUB_TOKEN=
HF_TOKEN=
ADMIN_ID=
```

Set `REPO_NAME` and `FILE_PATH` in [main.py](main.py) to a dedicated test repository and HTML file. The current implementation expects the `main` branch and markers such as `<!-- INSERT_AI_HERE -->` for the target categories. Give the GitHub token write access only to that test repository. Run `python main.py` and send the bot a non-sensitive sample link.

## Where to start in the code

- `clean_and_parse_json` and `analyze_content_full_cycle` handle model output, fallbacks and classification.
- `normalize_url` and `sync_push_to_github` handle basic duplicate checks and HTML publication.
- [filters.js](filters.js) implements filtering in the catalog.

## Project status

AI-assisted personal prototype. Message text is sent to an external inference service. Provider and model availability can change. Duplicate detection is a simplified HTML lookup. Classification quality and end-to-end reliability have not been benchmarked.
