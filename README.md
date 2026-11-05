# LCSC Ceramic Capacitors Scraper

A small Selenium project that downloads the whole Ceramic Capacitors category from LCSC as Excel files.

The problem is that LCSC only shows the first 5000 results of any search, and this category has way more parts than that. So the work is split in two scripts:

- `lcsc_phase1_planner_ceramic_cap.py` splits the category into groups of 5000 results or less, using filters like manufacturer, package and voltage. It only reads result counts and doesn't download anything.
- `lcsc_phase2_scraper_ceramic_cap.py` goes through those groups and downloads each one, 100 parts per page.

## What you need

- Python 3.9 or newer
- Google Chrome
- `pip install selenium pyperclip`

## Setup

Open both scripts and change the paths at the top:

- `OUTPUT_DIR` in phase 1 and `BASE_DIR` in phase 2 should be the same folder. The plan and all downloads go there.
- `CHROME_PROFILE_DIR` should be the same in both scripts.

Close any Chrome window that is using that profile before you run a script, otherwise Chrome will say the profile is already in use.

## Running it

Phase 1 first:

```
python lcsc_phase1_planner_ceramic_cap.py
```

It checks the result count for each filter value, packs values together until a group would go over 5000, and splits anything too big by the next filter. Every finished group is saved straight away to `_group_plan.json`, so you can stop it whenever you want and run the same command again. Groups already saved are skipped.

If LCSC blocks you with a 403, the script waits 15 minutes, then 30, then 60, and gives up after three blocks in a row. Wait a while and run it again.

When it's done, run phase 2:

```
python lcsc_phase2_scraper_ceramic_cap.py
```

For each group it opens the category page, applies the group's saved filters, sets the page size to 100 and clicks Download Table on every page. It is also resumable. Pages that are already downloaded are skipped, and finished groups get a flag file in `_state`.

Phase 2 doesn't wait out 403s on its own. If it keeps failing, stop it, wait 30 to 60 minutes and run it again.

### Saved links

Each group in the plan also has a Copy Link URL, but LCSC deletes those after 72 hours and you get a "Link Expired" page. So by default phase 2 ignores them and rebuilds every group from its filters. There are two flags if you want to use them anyway:

- `--use-saved-links` tries the saved link first and falls back to rebuilding if it expired.
- `--fresh-only` skips groups whose link has expired. You run it again without the flag afterwards to do the skipped ones.

## Output

```
_group_plan.json
_solo_count_cache.json
_state/                      done flags, temp downloads, debug screenshots
001_<group name>_page_001.xlsx
001_<group name>_page_002.xlsx
...
```

All the Excel files sit in one folder, named by group number, filters and page number.

## Notes

- 5000 is the maximum size of a group, not the target. Filter values are combined whole, so lots of groups end up smaller. One of my plans had 216 groups, about 579,000 parts and roughly 5,900 pages in total.
- In the plan, `capped: true` means LCSC showed more than 5000 results and the group was saved as 5000. `suspect: true` means the count failed a sanity check. Look at those groups after downloading and compare the row count with the planned count.
- Roughly 10 to 15 seconds per page, so the full run takes about 16 to 25 hours plus any 403 waiting. That is only an estimate, so time the first group.
- If Download Table or the page size dropdown can't be found, check the screenshots in `_state`. LCSC may have changed the page layout and the selectors might need updating.

Be nice to the site and keep the delays as they are. LCSC blocked the planner when the delay was 0.6 seconds. Also check their terms of use before running this at scale.
