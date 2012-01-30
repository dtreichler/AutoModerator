# Introduction
This is a bot for [reddit](http://www.reddit.com), meant to automate straightforward moderation tasks by automatically performing actions based on defined conditions.

If you'd like to utilize this functionality without running your own instance, just add AutoModerator as a mod to any subreddits you'd like to use it in, and [send a message to me (Deimorz on reddit)](http://www.reddit.com/message/compose/?to=Deimorz) explaining the conditions you'd like set up.

## Disclaimer

This code has been made public largely for the purpose of displaying exactly what AutoModerator does, and as an example for others working on reddit bots (moderation-related or otherwise). It may be difficult for others to get functional. Use at your own risk.

# Requirements
* [mellort / bboe's reddit api wrapper](http://pypi.python.org/pypi/reddit)  - at least version 1.2.4
* [Flask (for future web interface)](http://pypi.python.org/pypi/Flask)
* [Flask-SQLAlchemy](http://pypi.python.org/pypi/Flask-SQLAlchemy)
* [BeautifulSoup](http://pypi.python.org/pypi/BeautifulSoup)

# Setup
Copy modbot.cfg.example to modbot.cfg and edit values to match your desired database and reddit account. You can have SQLAlchemy create the tables for you by importing models.py into a Python interpreter session and calling `db.create_all()`.

Add the bot's account as a moderator to any subreddits you want it to check, then add those subreddits to the `subreddits` table and the desired conditions to `conditions`. (See below for examples of conditions)

I run it using a cronjob that checks through the list of subreddits every 5 minutes, but it would also be possible to run in an infinite loop, so that each subreddit is checked as often as possible.

# Condition Examples

### Remove submissions using common URL-shorteners

* subject = `submission`
* attribute = `domain`
* value = `(bit\.ly|goo\.gl|tinyurl\.com|t\.co|tiny\.cc)`
* action = `remove`

### Approve self-posts by not-extremely-new users 

* subject = `submission`
* attribute = `domain`
* value = `self\.subredditname`
* min\_account\_age = `3`
* min\_combined\_karma = `10`
* action = `approve`

### Allow only submissions from reddit.com and self-posts (e.g. /r/bestof)

**Condition #1**

* subject = `submission`
* attribute = `domain`
* value = `(reddit\.com|self\.bestof)`
* action = `approve`

**Condition #2**

* subject = `submission`
* attribute = `domain`
* value = `(reddit\.com|self\.bestof)`
* inverse = `true`
* action = `remove`

