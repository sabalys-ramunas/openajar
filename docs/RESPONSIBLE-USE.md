# Responsible use

openajar drives real, logged-in accounts. That makes it useful, and it makes the person who
runs it responsible for what those accounts do. These are the rules the project is built
around. By using openajar you agree to follow them.

## Your own accounts

Use openajar with accounts that are yours, or that you are authorised to run (your company's
pages, a client who asked you to). Never with accounts you bought, borrowed without
permission, or took over.

## Each platform's rules

Every site has terms of service and rules for automation. Read them for each site you
connect, and stay within them. Some sites forbid automation outright, some allow it with
limits, some offer an official API you should use instead. If a site tells you to stop, stop.

openajar makes a lane look like an ordinary computer so that a real person's account keeps
working when it moves off their laptop. It is not a tool for getting around a ban, a block,
a rate limit or a verification step. When a site challenges a lane (a captcha, a checkpoint,
a rate limit), openajar stops that lane and hands it to you. Do not build around that.

There is one exception, and it is narrow. In an environment's browser on its own machine, when
a site shows an "are you human" checkbox, the agent may click it once, as a person at that
browser would. openajar solves nothing there: an image puzzle, a checkpoint or a rate limit
still stops the browser and goes to you, and an account's lane is held for any challenge, the
checkbox included.

## No spam

- Do not send messages, comments or connection requests that people did not ask for, at a
  volume or in a pattern no person would.
- Do not post the same content across many accounts or many places to game a ranking.
- Do not fake engagement: no likes, follows, reviews or comments meant to make something
  look more popular than it is.

## No mass accounts

One person or one business, with the handful of accounts it really has. openajar is not for
creating accounts in bulk, running networks of fake personas, or operating accounts that
pretend to be different people.

## No harm to people

No scraping people's personal data, no tracking or profiling individuals, no harassment, no
impersonation, no fraud. Do not use openajar to collect credentials or payment details from
anyone.

## What the project will not build

Features whose main purpose is to break these rules (mass account creation, detection
evasion for banned accounts, spam at scale, scraping people) will not be merged. See
[CONTRIBUTING.md](../CONTRIBUTING.md).

## Reporting abuse

If you see openajar used against these rules, or find a way it makes abuse easier than it
should be, tell the maintainers ([SECURITY.md](../SECURITY.md) has the private channel).
