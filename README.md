# blogbot

A Telegram bot that listens to new messages in a channel, filters them out
(flood, isolated links, empty media) and publishes posts to a blog on the site
as `.mdx` files.

## How it works

The bot polls the channel through a **local** [telegram-bot-api](https://github.com/tdlib/telegram-bot-api)
instance (`http://127.0.0.1:8082`), with no external calls to `api.telegram.org`.
For every suitable message (and media album) it:

1. downloads media into a temp directory and "commits" it into the site
   (`public/media/...`);
2. builds paragraphs from the text, honoring entities (links, bold, code, etc.);
3. writes a post to `content/mdx/{ru,en}/blog/<slug>.mdx` using the shared
   `blog_common.py` module of the site. Posts are picked up on the fly, with no
   site rebuild needed.

Messages missed while the bot was down are re-read (telegram-bot-api keeps
unacknowledged updates for ~24h). Deduplication is based on `message_id` in the
state file.

## Setup and run

The environment and dependencies (python-telegram-bot) are described in
`shell.nix`:

```sh
nix-shell
python blogbot.py
```

## Configuration

Everything is configured via environment variables (see `.env.example`):

| Variable                | Default                                                    | Description                            |
| ----------------------- | ---------------------------------------------------------- | -------------------------------------- |
| `BLOGBOT_TOKEN`         | required (from sops)                                       | bot token, **never stored in files**   |
| `BLOGBOT_BOT_API_URL`   | `http://127.0.0.1:8082`                                    | local telegram-bot-api address         |
| `BLOGBOT_CHANNEL_ID`    | `-1001667272666`                                           | channel to read from                   |
| `BLOGBOT_SITE_ROOT`     | `~/files/mounts/TS480SSD/services/site/d7tun6`             | site root (where `blog_common.py` is)  |
| `BLOGBOT_STATE`         | `./blogbot-state.json`                                     | state file (dedup, slugs)              |
| `BLOGBOT_TMP`           | `./tmp`                                                    | temp directory for media downloads     |

Example:

```sh
export BLOGBOT_TOKEN="$(sops -d secrets/blogbot.yaml | jq -r .blogbot_token)"
python blogbot.py --poll-sleep 1.0
```

## Filtering (what ends up in the blog)

- non-media messages shorter than 50 characters (flood);
- isolated links without context;
- "media without text/context";
- empty messages.

## Structure

```
blogbot.py         all bot logic
shell.nix          dev environment (nix)
.env.example       environment variable template
blogbot-state.json runtime state (in .gitignore, never in the repo)
```

## Security

The bot token is passed **only** through the `BLOGBOT_TOKEN` environment
variable (from sops). The repository never contains: `.env`, the state file,
temp media files, Python caches. All secrets and garbage are listed in
`.gitignore`.