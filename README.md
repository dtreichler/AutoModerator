# Introduction
This is a bot for [reddit](http://www.reddit.com), meant to automate straightforward moderation tasks by automatically performing actions based on defined conditions.

I run it using a cronjob that checks through the list of subreddits every 5 minutes, but it would also be possible to run in an infinite loop, so that each subreddit is checked as often as possible.

## Disclaimer

This code has been made public largely for the purpose of displaying exactly what AutoModerator does, and as an example for others working on reddit bots (moderation-related or otherwise). It may be difficult for others to get functional. Use at your own risk.

# Requirements
* [mellort / bboe's reddit api wrapper](https://github.com/mellort/reddit_api)  - at least version 1.2.4
* Flask (for future web interface)
* Flask-SQLAlchemy
* BeautifulSoup

# Setup
Copy modbot.cfg.example to modbot.cfg and edit values to match your desired database and reddit account. You can have SQLAlchemy create the tables for you by importing models.py into a Python interpreter session and calling `db.create_all()`.

# Condition Examples
*Coming soon, will show examples of conditions the bot uses.*

