# Insight Pixels Opportunity Scanner

Collects new consultancy and tender postings every week so nobody has to check
each site by hand. It runs on GitHub for free and uses no paid services.

## What it does

Every Monday it:

1. Pulls new consultancy postings from **ReliefWeb** (official API).
2. Pulls new Requests for Expression of Interest from **World Bank procurement
   notices** (official API).
3. Checks each **watched page** (for example the CRS and Counterpart bid pages)
   and reports any link that was not there last week.
4. Drops anything already collected or already closed.
5. Saves the new postings in `data/weekly/` as a spreadsheet-friendly `.csv`
   and a `.json` file with the full posting text.

It adds simple flags to help triage: short runway (under 10 days), individual
consultant selection, and postings not listed as consultancies.

It does **not** decide what to pursue. The weekly file is then screened against
the Insight Pixels criteria (service-line fit, sector, eligibility, registration
requirements) with Claude, and a person makes the final call.

## One-time setup

1. **Create the repository.** On GitHub, create a new private repository and
   upload everything in this folder, including the hidden `.github` folder.
2. **Request a ReliefWeb app name.** ReliefWeb requires a pre-approved app name
   on every API call. It is free. Request one through the ReliefWeb API
   documentation (https://apidoc.reliefweb.int/).
3. **Add the app name as a secret.** In the repository: Settings, Secrets and
   variables, Actions, New repository secret. Name it `RELIEFWEB_APPNAME`.
   Until this is set, ReliefWeb is skipped and the other sources still run.
4. **Run it once by hand.** Actions tab, "Weekly opportunity collection",
   Run workflow. For the first run, enter 30 in the days box to catch up.

## Adding a site

Open `config.json` and add an entry under `watched_pages`:

    {"name": "Save the Children RFPs", "url": "https://..."}

If a page produces too many irrelevant links, add a `link_pattern` so only
matching links are kept, for example `"link_pattern": "rfp|tender|proposal|tor"`.

The first time a page is scanned, every link on it counts as new. From the
second week on, only new links appear.

Watched pages only work for plain pages that list opportunities as links.
Sites that need a login or load their listings with JavaScript (Devex,
DevelopmentAid, Impactpool, Workday portals) cannot be collected this way.

## Other settings in config.json

- `lookback_days`: how far back each run looks (8, so weekly runs overlap).
- `short_runway_days`: deadline threshold for the short runway flag.
- `keywords`: terms used only to sort the weekly file, most relevant first.
  Nothing is excluded for lacking a keyword.
- `reliefweb.title_terms`: titles that get collected even when a posting is
  not listed as a consultancy.
- `worldbank.notice_types`: notice types to keep.

## Running it on your own computer

Needs Python 3.9 or later. Nothing to install.

    python collect.py              # normal run
    python collect.py --days 30    # look further back
    python collect.py --debug      # print one raw record per source
    python -m unittest tests/test_collect.py
