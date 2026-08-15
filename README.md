# 1st Energy for Home Assistant

Unofficial integration for Australian [1st Energy](https://www.1stenergy.com.au)
electricity accounts. Feeds hourly consumption and cost into the Home Assistant
Energy dashboard, and exposes account balance and invoice details as sensors.

> **This uses a private API.** 1st Energy publish no public API. Everything here
> was reverse-engineered from their customer portal, and they may change or block
> it at any time, without notice and without any obligation to keep it working.
> Nothing here is endorsed by or affiliated with 1st Energy.

## What you get

**In the Energy dashboard** — hourly electricity consumption and hourly cost,
both backdated to the hours they actually occurred, at the meter's native
5-minute resolution aggregated to hours. Time-of-use bands are read from the
meter data, so a time-of-use plan is costed the way the retailer costs it rather
than against a flat rate you type in.

**As sensors** — account balance, next invoice amount, next invoice due date,
and the date the meter data currently reaches.

## The one-day lag

1st Energy's meter data runs roughly a day behind. There is no live power
reading available through this API and no amount of polling will produce one.

That shapes the whole integration. Consumption is written as *long-term
statistics* with historical timestamps rather than published as a sensor state,
because a sensor would record yesterday's kilowatt-hours against today's clock
and quietly corrupt every total in the Energy dashboard.

Practical consequences:

- Today's usage will not appear. Yesterday's generally will.
- The "Meter data up to" sensor tells you how far the data actually reaches, so
  an empty-looking dashboard can be recognised as normal rather than broken.
- If you want live power, this cannot give it to you. A pulse or optical reader
  on the meter itself is the only route, with this integration reconciling cost
  behind it.

## Installation

### HACS (recommended)

1. HACS → three-dot menu → **Custom repositories**
2. Add `https://github.com/FoxClock/1stEnergy-Assistant`, category **Integration**
3. Install **1st Energy**, then restart Home Assistant
4. **Settings → Devices & services → Add integration → 1st Energy**

### Manual

Copy `custom_components/first_energy/` into your Home Assistant `config/custom_components/`
directory and restart.

## Setup

Sign in with the same email address and password you use at
`myaccount.1stenergy.com.au`. If the login covers more than one electricity
account you will be asked which to add; repeat the process to add the others.

On first run the integration walks backwards through your available history in
the background and imports all of it. How much exists depends on how long you
have been with 1st Energy — it stops automatically when the data runs out.
Recent data lands first, so the Energy dashboard becomes useful immediately
rather than only when the whole backfill finishes.

### Adding it to the Energy dashboard

**Settings → Dashboards → Energy → Grid consumption → Add consumption**, then
pick `1st Energy energy …`. When asked about cost, choose **Use an entity
tracking the total costs** and select `1st Energy cost …` — the retailer's own
figures, including time-of-use bands, rather than an estimate.

## Polling

Every six hours. The data only moves once a day, but a single daily poll could
add most of another day of latency depending on when the retailer's overnight
load lands. Each poll re-requests the last few days; rewriting hours already
held costs nothing and quietly repairs any gap left by a failed poll or a
Home Assistant outage.

## Security

Your 1st Energy password is stored in the Home Assistant config entry, which
lives as plain JSON under `config/.storage/`. That is standard for integrations
that must log in with a password, but it is worth knowing: anyone who can read
that directory can read the password.

If you have captured HAR files while investigating the API yourself, they
contain your password, live tokens, NMI and address in plain text. Keep them out
of version control — `.gitignore` here already excludes `*.har`.

## Development

```bash
uv sync --group dev
uv run pytest
uv run ruff check custom_components tests
```

Tests run against real payloads captured from a live account and sanitised
(`dev/capture_fixtures.py` regenerates them; `dev/rescrub.py` re-applies
redaction). The Home Assistant layer is tested against a real recorder database
via `pytest-homeassistant-custom-component`.

The client under `custom_components/first_energy/api/` and the models under
`domain/` import nothing from Home Assistant, so they can be tested — and
reused — on their own.

## Licence

MIT.
