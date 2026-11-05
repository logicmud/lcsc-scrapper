"""
LCSC Phase 1 - Filter Group Planner
------------------------------------
For a category with MORE than 5000 results (LCSC only ever shows the first
5000 of a result set, no matter how you paginate), this script builds a
resumable "group plan": a list of filter combinations, each guaranteed to
return <=5000 results, together with a Copy-Link URL for that combination.

Algorithm (greedy bin-packing, recursive across facets):
  1. Take the facet list in FACET_PRIORITY order (default: Manufacturer,
     Package, Packaging).
  2. For the current facet, read every value's SOLO result count (within
     whatever parent scope is already locked in via `applied_filters`).
  3. Walk the values in DOM order, accumulating them into the current
     group. As soon as adding the next value would push the running total
     over MAX_PER_GROUP, close the current group and start a new one with
     the value that would have overflowed.
  4. If a single value's solo count already exceeds MAX_PER_GROUP, it
     becomes its own scope and we recurse into the NEXT facet inside that
     scope (e.g. Manufacturer=X is too big -> split X by Package).
  5. Every finalized group (<=5000) gets its filters actually applied via
     "Apply All", then "Copy Link" is clicked and the URL is read from the
     clipboard, and the group is appended to the output JSON immediately
     (so a crash never loses completed work).

Requirements:
    pip install selenium pyperclip

IMPORTANT - selector confidence:
  The selectors below are built from HTML snippets you already sent
  (manufacturer row, Apply All button, Results counter, checked-state
  svg, Copy Link) plus the visible layout in your screenshot. The exact
  wrapping container for each filter COLUMN (the box that holds the
  "Manufacturer" / "Package" / "Packaging" header + Search Filter input +
  list) was never captured, so `find_facet_container()` below uses a
  best-effort text-based search with a debug dump on failure. If it fails,
  right-click the border of that column -> Inspect -> Copy outerHTML and
  send it back, same as before - one HTML snippet fixes it everywhere
  since every facet column reuses the same Vue component.
"""

import os
import json
import re
import time
import random
import shutil
import pyperclip
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    ElementClickInterceptedException,
    StaleElementReferenceException,
)

# ----------------------------- CONFIG ---------------------------------

CATEGORY_URL = "https://www.lcsc.com/category/1142.html"  # Ceramic Capacitors
CATEGORY_LABEL = "Ceramic Capacitors"

OUTPUT_DIR = r"C:\Users\ruchi\OneDrive\Desktop\DIACSIDAC\Ceramic Capacitors"
PLAN_PATH = os.path.join(OUTPUT_DIR, "_group_plan.json")
SOLO_COUNT_CACHE_PATH = os.path.join(OUTPUT_DIR, "_solo_count_cache.json")

MAX_PER_GROUP = 5000

# Set > 0 to skip manufacturers with <= this many results (download by hand).
# 0 = keep all manufacturers. Small ones are grouped together up to 5000 rows.
MIN_MANUFACTURER_COUNT = 0   # 0 = plan EVERY manufacturer (small ones get bin-packed together)

# Order in which facets are tried. Only facets that render as a
# checkbox list (Search Filter + rows), not Min/Max numeric facets,
# belong here. Add more names if Manufacturer/Package/Packaging still
# isn't enough to get every scope under 5000.
FACET_PRIORITY = [
    "Manufacturer", "Package", "Packaging", "Temperature Coefficient",
    "Voltage Rating", "Capacitance", "Tolerance", "Operating Temperature",
    "Pin Spacing", "Voltage(AC)", "Ratings", "safety certification",
    "Body Thickness",
]
# NOTE: a facet that doesn't exist / has no values in the current scope is
# skipped automatically and the next one in the list is tried.

CHROME_PROFILE_DIR = r"C:\Users\ruchi\ChromeSeleniumProfile"  # persistent profile, reused by Phase 2 too
PAGE_LOAD_TIMEOUT = 20
CLICK_SETTLE_DELAY = 1.8   # seconds to wait after Apply All for table+counter to refresh
POLITE_DELAY = 6.0         # base delay between checks (+ random 0-1.5s jitter). LCSC 403-blocked us at 0.6s.
BLOCK_COOLDOWNS = [900, 1800, 3600]   # seconds to sleep after a 403, per consecutive block

# ------------------------------------------------------------------------


def build_driver():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    options = Options()
    options.add_argument("--start-maximized")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    if CHROME_PROFILE_DIR:
        options.add_argument(f"--user-data-dir={CHROME_PROFILE_DIR}")
    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    })
    return driver


def dump_debug_info(driver, label):
    try:
        shot_path = os.path.join(OUTPUT_DIR, f"debug_{label}.png")
        html_path = os.path.join(OUTPUT_DIR, f"debug_{label}.html")
        driver.save_screenshot(shot_path)
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        print(f"[debug] Saved screenshot -> {shot_path}")
        print(f"[debug] Saved page HTML  -> {html_path}")
    except Exception as e:
        print(f"[debug] Could not save debug info: {e}")


class BlockedError(Exception):
    """LCSC answered 403 Forbidden (rate-limit / WAF block)."""


def page_state(driver):
    """(title, html_length, first 200 chars of visible text) - for diagnosing odd pages."""
    try:
        return driver.execute_script(
            "return [document.title || '', document.documentElement.outerHTML.length,"
            " ((document.body && document.body.innerText) || '').replace(/\\s+/g,' ').trim().slice(0,200)];"
        )
    except Exception:
        return ("", 0, "")


_BLOCK_WORDS = ("403 forbidden", "access denied", "too many requests", "just a moment",
                "attention required", "verify you are human", "captcha",
                "error 1015", "rate limit", "unusual traffic")


def is_blocked(driver):
    """True if the page looks like a block / challenge / error page instead of
    the real category page (403, 429, Cloudflare, captcha, empty shell...)."""
    title, size, text = page_state(driver)
    blob = (title + " " + text).lower()
    if any(w in blob for w in _BLOCK_WORDS):
        return True
    if re.search(r"\b(403|429)\b", title):
        return True
    # real category page is huge and always contains the filter headers
    if size < 8000 and "manufacturer" not in blob:
        return True
    return False


def polite_sleep():
    time.sleep(POLITE_DELAY + random.uniform(0, 1.5))


# --------------------------- JSON checkpoints ---------------------------

def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def filters_key(filters: dict) -> str:
    """Stable string key for a filters dict, used for caching/resuming."""
    parts = []
    for facet in sorted(filters.keys()):
        parts.append(facet + "=" + "|".join(sorted(filters[facet])))
    return ";;".join(parts)


# --------------------------- facet DOM helpers ---------------------------

FACET_WAIT = 15   # seconds to keep looking for a facet column after a page re-render


def _find_facet_container_once(driver, facet_name):
    """Return the scrollable value list (div.v2-scroll-style) that belongs to
    the filter column whose bold header has title=<facet_name>.
    Min/Max boxes and the 'Search Filter' box are simply ignored."""
    lit = _xpath_literal(facet_name)
    headers = driver.find_elements(
        By.XPATH,
        f"//div[@title={lit}][contains(@class,'font-Bold')]"
        f"[not(contains(@class,'filterItemShowTit'))]",
    )
    for header in headers:
        for list_xpath in (
            "following::div[contains(@class,'v2-scroll-style')][1]",
            "following::div[contains(@class,'filterItemContent')][1]",
        ):
            try:
                lst = header.find_element(By.XPATH, list_xpath)
                # Guard: that list must really belong to THIS header. If this
                # facet has no list, 'following' would grab the NEXT column's.
                owner = lst.find_element(
                    By.XPATH, "preceding::div[@title][contains(@class,'font-Bold')][1]"
                ).get_attribute("title")
                if owner == facet_name:
                    return lst
            except (NoSuchElementException, StaleElementReferenceException):
                continue
    return None


def find_facet_container(driver, facet_name):
    """Locate the wrapping box for a named filter column. LCSC re-renders the
    filter panel after every Apply/Reset, so keep polling for a few seconds
    before giving up."""
    end_time = time.time() + FACET_WAIT
    while time.time() < end_time:
        if is_blocked(driver):
            raise BlockedError("403 Forbidden from LCSC")
        try:
            container = _find_facet_container_once(driver, facet_name)
        except StaleElementReferenceException:
            container = None
        if container is not None:
            return container
        time.sleep(0.5)

    dump_debug_info(driver, f"facet_container_not_found_{facet_name}")
    raise NoSuchElementException(
        f"Could not locate the '{facet_name}' filter column after {FACET_WAIT}s."
    )


def scroll_container_to_bottom(driver, container):
    """In case the value list is lazy-rendered, scroll until item count
    stabilizes. If it's already fully rendered (expected), this is a
    harmless no-op after one iteration."""
    last_count = -1
    stable_rounds = 0
    for _ in range(40):
        rows = container.find_elements(By.CSS_SELECTOR, "div.filterItemShowTit")
        count = len(rows)
        if count == last_count:
            stable_rounds += 1
            if stable_rounds >= 2:
                break
        else:
            stable_rounds = 0
        last_count = count
        driver.execute_script(
            "arguments[0].scrollTop = arguments[0].scrollHeight;", container
        )
        time.sleep(0.15)


def get_facet_values(driver, facet_name):
    """Return the list of value names (in DOM/display order) for a facet."""
    container = find_facet_container(driver, facet_name)
    scroll_container_to_bottom(driver, container)
    rows = container.find_elements(By.CSS_SELECTOR, "div.filterItemShowTit")
    values = []
    for row in rows:
        title = row.get_attribute("title") or row.text.strip()
        if title:
            values.append(title)
    return values


def _find_value_row(driver, facet_name, value):
    container = find_facet_container(driver, facet_name)
    xpath = f".//div[contains(@class,'filterItemShowTit') and @title={_xpath_literal(value)}]"
    return container.find_element(By.XPATH, xpath)


def _xpath_literal(s):
    """Safely quote a string (which may itself contain quotes) for XPath."""
    if '"' not in s:
        return f'"{s}"'
    if "'" not in s:
        return f"'{s}'"
    parts = s.split('"')
    return "concat(" + ', \'"\', '.join(f'"{p}"' for p in parts) + ")"


def is_value_checked(driver, facet_name, value):
    try:
        title_row = _find_value_row(driver, facet_name, value)
        wrapper = title_row.find_element(By.XPATH, "./ancestor::div[contains(@class,'filterItemShow')][1]")
        checks = wrapper.find_elements(
            By.XPATH, ".//*[local-name()='use'][@*[local-name()='href']=\"#icon-wmsc-check\"]"
        )
        return len(checks) > 0
    except NoSuchElementException:
        return False


def toggle_facet_value(driver, facet_name, value):
    """Click a value row to flip its checked state."""
    title_row = _find_value_row(driver, facet_name, value)
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", title_row)
    try:
        title_row.click()
    except (ElementClickInterceptedException, StaleElementReferenceException):
        driver.execute_script("arguments[0].click();", title_row)


def set_facet_selection(driver, facet_name, desired_values):
    """Make the checked set for a facet exactly equal desired_values."""
    current_values = get_facet_values(driver, facet_name)
    desired_set = set(desired_values)
    for value in current_values:
        checked = is_value_checked(driver, facet_name, value)
        should_be_checked = value in desired_set
        if checked != should_be_checked:
            toggle_facet_value(driver, facet_name, value)


# --------------------------- apply / reset / read ---------------------------

def dismiss_cookie_banner(driver):
    """Click 'Accept all cookies' if the consent banner is showing, so it
    can't sit on top of and intercept clicks on filter controls."""
    xpaths = [
        "//*[self::button or self::a][normalize-space(text())='Accept all cookies']",
    ]
    for xp in xpaths:
        try:
            el = WebDriverWait(driver, 5).until(EC.element_to_be_clickable((By.XPATH, xp)))
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
            try:
                el.click()
            except ElementClickInterceptedException:
                driver.execute_script("arguments[0].click();", el)
            time.sleep(0.5)
            return
        except TimeoutException:
            continue
    # Banner likely wasn't present (e.g. already dismissed earlier) - fine.


def click_reset_all(driver):
    xpaths = [
        "//*[self::button or self::a or self::span][normalize-space(text())='Reset All']",
    ]
    for xp in xpaths:
        try:
            el = WebDriverWait(driver, 5).until(EC.element_to_be_clickable((By.XPATH, xp)))
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
            try:
                el.click()
            except ElementClickInterceptedException:
                driver.execute_script("arguments[0].click();", el)
            time.sleep(CLICK_SETTLE_DELAY)
            return
        except TimeoutException:
            continue
    print("[warn] 'Reset All' control not found - filters may carry over between checks.")
    dump_debug_info(driver, "reset_all_not_found")


def click_apply_all(driver):
    xpaths = [
        "//button[.//span[normalize-space(text())='Apply All']]",
        "//*[self::button or self::a][normalize-space(text())='Apply All']",
    ]
    for xp in xpaths:
        try:
            el = WebDriverWait(driver, 5).until(EC.element_to_be_clickable((By.XPATH, xp)))
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
            try:
                el.click()
            except ElementClickInterceptedException:
                driver.execute_script("arguments[0].click();", el)
            time.sleep(CLICK_SETTLE_DELAY)
            return
        except TimeoutException:
            continue
    dump_debug_info(driver, "apply_all_not_found")
    raise NoSuchElementException("Could not find the 'Apply All' button.")


def read_results_count(driver):
    """
    Read the post-Apply-All 'Results: X of Y' number. Falls back to the
    table's 'Showing 1-100 of X' line if the filter-panel counter isn't
    present (e.g. no filters currently active).
    """
    # Primary: filter-panel counter, e.g. "Results: 6,656 of 31,893"
    try:
        results_word = driver.find_element(
            By.XPATH, "(//*[contains(text(),'Results:')])[1]"
        )
        number_el = results_word.find_element(
            By.XPATH,
            "following::span[contains(@class,'font-Bold')][1]",
        )
        return int(number_el.text.strip().replace(",", ""))
    except (NoSuchElementException, ValueError):
        pass

    # Fallback: "Showing 1-100 of 6,656"
    try:
        showing_el = driver.find_element(By.XPATH, "//*[contains(text(),'of')][contains(text(),'Showing') or ../*[contains(text(),'Showing')]]")
        text = showing_el.text
    except NoSuchElementException:
        text = driver.find_element(By.TAG_NAME, "body").text

    import re
    match = re.search(r"of\s+([\d,]+)", text)
    if match:
        return int(match.group(1).replace(",", ""))

    if is_blocked(driver):
        raise BlockedError("403 Forbidden from LCSC while reading results count")
    dump_debug_info(driver, "results_count_not_found")
    raise NoSuchElementException("Could not read a results count from the page.")


def read_count_stable(driver, max_reads=6, gap=1.5):
    """read_results_count, repeated until two consecutive reads agree, so we
    don't record a stale number while LCSC is still refreshing."""
    last = None
    for _ in range(max_reads):
        cur = read_results_count(driver)
        if cur == last:
            return cur
        last = cur
        time.sleep(gap)
    return last


def _apply_filters_once(driver, filters: dict):
    """Reset everything, then check exactly the given values per facet,
    then click Apply All. `filters` is {facet_name: [value, ...]}.
    If `filters` is empty, there is nothing to select, and LCSC leaves
    the "Apply All" button disabled in that state - so we just reset
    and stop (the unfiltered full count is already showing)."""
    click_reset_all(driver)
    if not filters:
        return
    for facet_name, values in filters.items():
        set_facet_selection(driver, facet_name, values)
    click_apply_all(driver)


def apply_filters(driver, filters: dict, attempts=4):
    """_apply_filters_once with retries. On a 403 block, sleep a long
    cooldown (5 / 15 / 30 min) before reloading; on ordinary glitches just
    reload and retry."""
    last = None
    blocks = 0
    for attempt in range(1, attempts + 1):
        try:
            _apply_filters_once(driver, filters)
            return
        except BlockedError as e:
            last = e
            if blocks >= len(BLOCK_COOLDOWNS):
                raise
            wait_s = BLOCK_COOLDOWNS[blocks]
            blocks += 1
            print(f"  [BLOCKED] LCSC returned 403. Cooling down {wait_s // 60} min "
                  f"(block {blocks}/{len(BLOCK_COOLDOWNS)})...")
            time.sleep(wait_s)
        except (NoSuchElementException, StaleElementReferenceException,
                ElementClickInterceptedException, TimeoutException) as e:
            last = e
            t, n, txt = page_state(driver)
            print(f"  [retry {attempt}/{attempts}] apply_filters failed "
                  f"({type(e).__name__}). Page title={t!r}, html={n} chars, text={txt[:120]!r}")
            if attempt >= 2:
                # Repeated failures right after reloads usually mean LCSC is
                # throttling us, so stop hammering it.
                print("  [cooldown] repeated failures - waiting 3 min before reloading...")
                time.sleep(180)
            else:
                time.sleep(3)
        try:
            driver.get(CATEGORY_URL)
        except TimeoutException:
            pass
        time.sleep(3)
        dismiss_cookie_banner(driver)
    raise last


def copy_link_url(driver):
    sentinel = "__lcsc_clip_sentinel__"
    try:
        pyperclip.copy(sentinel)
    except Exception as e:
        print(f"[warn] Could not access clipboard via pyperclip ({e}); "
              f"install/verify pyperclip works on this machine.")

    xpaths = [
        "//*[self::button or self::a][.//span[normalize-space(text())='Copy Link']]",
        "//*[self::button or self::a][normalize-space(text())='Copy Link']",
    ]
    clicked = False
    for xp in xpaths:
        try:
            el = WebDriverWait(driver, 5).until(EC.element_to_be_clickable((By.XPATH, xp)))
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
            try:
                el.click()
            except ElementClickInterceptedException:
                driver.execute_script("arguments[0].click();", el)
            clicked = True
            break
        except TimeoutException:
            continue

    if not clicked:
        dump_debug_info(driver, "copy_link_not_found")
        raise NoSuchElementException("Could not find the 'Copy Link' button.")

    for _ in range(20):
        time.sleep(0.25)
        try:
            clip = pyperclip.paste()
        except Exception:
            clip = ""
        if clip and clip != sentinel:
            return clip.strip()

    print("[warn] Clipboard did not change after clicking Copy Link; "
          "falling back to the browser's current URL (may not encode all filters).")
    return driver.current_url


# --------------------------- bin packing ---------------------------

def greedy_bin_pack(values, counts, max_per_group):
    """
    values: ordered list of value names.
    counts: dict value -> solo count (within current scope).
    Returns a list of bins (each a list of values). A bin with exactly
    one value whose own count exceeds max_per_group signals "needs
    recursion into the next facet" to the caller.
    """
    bins = []
    current_bin = []
    current_sum = 0

    for value in values:
        c = counts[value]
        if c > max_per_group:
            # Doesn't fit anywhere by itself - close current bin (if any)
            # and emit this value as its own oversized bin for recursion.
            if current_bin:
                bins.append(current_bin)
                current_bin = []
                current_sum = 0
            bins.append([value])
            continue

        if current_sum + c > max_per_group:
            if current_bin:
                bins.append(current_bin)
            current_bin = [value]
            current_sum = c
        else:
            current_bin.append(value)
            current_sum += c

    if current_bin:
        bins.append(current_bin)

    return bins


# --------------------------- planning ---------------------------

def group_description(filters: dict) -> str:
    parts = []
    for facet in FACET_PRIORITY:
        if facet in filters:
            parts.append(f"{facet}={'+'.join(filters[facet])}")
    return "; ".join(parts) if parts else "ALL"


def get_solo_counts(driver, facet_name, values, applied_filters, cache, scope_total=None):
    """Solo count for each value, layered on top of applied_filters.
    Cached to disk so a crash mid-run doesn't lose progress."""
    scope_key = filters_key(applied_filters) + f"||facet={facet_name}"
    scope_cache = cache.setdefault(scope_key, {})
    counts = {}

    # A single value can never have more results than the scope it sits in.
    # A bigger number means the page wasn't really filtered (blocked/glitch).
    limit = (scope_total * 1.01 + 5) if scope_total else float("inf")

    for value in values:
        if value in scope_cache:
            if scope_cache[value] <= limit:
                counts[value] = scope_cache[value]
                continue
            print(f"    [cache] discarding impossible cached count for {value!r}: "
                  f"{scope_cache[value]} > scope total {scope_total}")
            del scope_cache[value]
        trial_filters = dict(applied_filters)
        trial_filters[facet_name] = [value]
        count = None
        for attempt in range(1, 4):
            apply_filters(driver, trial_filters)
            count = read_count_stable(driver)
            if is_blocked(driver):
                raise BlockedError("LCSC served a block page while counting")
            if count <= limit:
                break
            print(f"    [verify {attempt}/3] {value!r} read {count} but scope total is "
                  f"{scope_total}; retrying...")
            time.sleep(10)
        else:
            raise BlockedError(f"Counts look unreliable ({value!r}={count} > scope total "
                               f"{scope_total}) - page probably not filtering")
        counts[value] = count
        scope_cache[value] = count
        save_json(SOLO_COUNT_CACHE_PATH, cache)
        print(f"    [{facet_name}] {value!r}: {count}")
        polite_sleep()

    return counts


def finalize_group(driver, applied_filters, facet_name, bin_values, plan, expected=None):
    filters = dict(applied_filters)
    filters[facet_name] = bin_values
    key = filters_key(filters)

    if any(g["key"] == key for g in plan["groups"]):
        print(f"  [skip] Group already planned: {group_description(filters)}")
        return

    def looks_wrong(c):
        # Solo counts are an upper bound for the combined bin (values can
        # only overlap, never add). A bigger number = the filter didn't take.
        return expected is not None and c > expected * 1.02 + 10

    count = None
    for attempt in range(1, 4):
        apply_filters(driver, filters)
        count = read_count_stable(driver)
        if not looks_wrong(count):
            break
        print(f"  [verify {attempt}/3] {group_description(filters)} reads {count} "
              f"but expected <= {expected}; filter probably didn't apply - retrying...")
        time.sleep(5)
        try:
            driver.get(CATEGORY_URL)
        except TimeoutException:
            pass
        time.sleep(3)
        dismiss_cookie_banner(driver)

    suspect = looks_wrong(count)
    capped = count > MAX_PER_GROUP
    if suspect:
        print(f"  [warn] still mismatched after retries ({count} vs expected <= {expected}); "
              f"flagging group as suspect.")
    if capped:
        print(f"  [capped] {count} results; Phase 2 will take the first {MAX_PER_GROUP}.")

    url = copy_link_url(driver)
    group = {
        "key": key,
        "filters": filters,
        "description": group_description(filters),
        "count": min(count, MAX_PER_GROUP),
        "actual_count": count,
        "capped": capped,
        "suspect": suspect,
        "url": url,
    }
    plan["groups"].append(group)
    save_json(PLAN_PATH, plan)
    print(f"  [saved] {group['description']} -> {count} results")
    polite_sleep()


def finalize_capped_group(driver, applied_filters, plan):
    """Every facet is used up and the scope is STILL > MAX_PER_GROUP.
    LCSC only ever serves the first 5000 rows of a result set, so record the
    group anyway with its count capped at MAX_PER_GROUP - Phase 2 then
    downloads those first 5000."""
    key = filters_key(applied_filters)
    if any(g["key"] == key for g in plan["groups"]):
        print(f"  [skip] Capped group already planned: {group_description(applied_filters)}")
        return

    apply_filters(driver, applied_filters)
    actual = read_count_stable(driver)
    url = copy_link_url(driver)
    group = {
        "key": key,
        "filters": applied_filters,
        "description": group_description(applied_filters),
        "count": min(actual, MAX_PER_GROUP),
        "actual_count": actual,
        "capped": actual > MAX_PER_GROUP,
        "url": url,
    }
    plan["groups"].append(group)
    save_json(PLAN_PATH, plan)
    print(f"  [saved-CAPPED] {group['description']} -> {actual} results "
          f"(downloading first {group['count']})")
    polite_sleep()


def plan_for_scope(driver, facet_chain, applied_filters, plan, cache):
    if not facet_chain:
        print(f"  [capped] Ran out of facets for scope "
              f"{group_description(applied_filters)} - taking the first "
              f"{MAX_PER_GROUP} results.")
        finalize_capped_group(driver, applied_filters, plan)
        return

    facet_name = facet_chain[0]
    rest = facet_chain[1:]

    apply_filters(driver, applied_filters)
    try:
        values = get_facet_values(driver, facet_name)
    except NoSuchElementException:
        print(f"  [warn] Facet column '{facet_name}' not found on page.")
        values = []
    applied_vals = {v for vs in applied_filters.values() for v in vs} - {"-"}
    if values and (set(values) & applied_vals):
        print(f"  [warn] Facet '{facet_name}' returned already-applied values "
              f"{sorted(set(values) & applied_vals)[:3]} - wrong column read, skipping facet.")
        values = []
    if not values:
        print(f"  [warn] No values found for facet '{facet_name}' in scope "
              f"{group_description(applied_filters)} - trying next facet.")
        plan_for_scope(driver, rest, applied_filters, plan, cache)
        return

    print(f"[{facet_name}] {len(values)} values in scope "
          f"{group_description(applied_filters)}")
    scope_total = read_count_stable(driver)
    counts = get_solo_counts(driver, facet_name, values, applied_filters, cache, scope_total)

    if facet_name == "Manufacturer" and MIN_MANUFACTURER_COUNT > 0:
        before = len(values)
        values = [v for v in values if counts[v] > MIN_MANUFACTURER_COUNT]
        skipped = before - len(values)
        print(f"  [filter] Keeping {len(values)} manufacturer(s) with "
              f"> {MIN_MANUFACTURER_COUNT} results; skipping {skipped} "
              f"smaller one(s) - download those manually.")
        if not values:
            print("  [warn] No manufacturers exceeded the threshold - nothing to plan.")
            return

    bins = greedy_bin_pack(values, counts, MAX_PER_GROUP)

    for b in bins:
        bin_sum = sum(counts[v] for v in b)
        if len(b) == 1 and counts[b[0]] > MAX_PER_GROUP:
            # Needs recursion into the next facet within this single value.
            value = b[0]
            next_scope = dict(applied_filters)
            next_scope[facet_name] = [value]
            print(f"  [recurse] {facet_name}={value!r} has {counts[value]} "
                  f"(> {MAX_PER_GROUP}); splitting by next facet.")
            plan_for_scope(driver, rest, next_scope, plan, cache)
        else:
            finalize_group(driver, applied_filters, facet_name, b, plan, expected=bin_sum)


def clean_old_data(plan, cache):
    """Only remove groups written by the very first buggy version: they have no
    'capped' field and a count above the 5000 limit. Everything else (including
    Voltage Rating / Capacitance groups made by the fixed column finder) is kept."""
    before = len(plan["groups"])
    plan["groups"] = [
        g for g in plan["groups"]
        if not ("capped" not in g and g["count"] > MAX_PER_GROUP)
    ]
    dropped = before - len(plan["groups"])
    if dropped:
        print(f"[clean] removed {dropped} invalid group(s) from the earliest buggy run.")
        save_json(PLAN_PATH, plan)


def backup_state():
    """Copy the plan + count cache into _backups\\<timestamp>\\ at every start,
    so a bad run can never destroy earlier progress."""
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(OUTPUT_DIR, "_backups", stamp)
    for p in (PLAN_PATH, SOLO_COUNT_CACHE_PATH):
        if os.path.exists(p):
            os.makedirs(dest, exist_ok=True)
            shutil.copy2(p, dest)
    if os.path.isdir(dest):
        print(f"[backup] saved plan + cache to {dest}")


def main():
    plan = load_json(PLAN_PATH, {"category_url": CATEGORY_URL,
                                  "category_label": CATEGORY_LABEL,
                                  "max_per_group": MAX_PER_GROUP,
                                  "groups": []})
    cache = load_json(SOLO_COUNT_CACHE_PATH, {})
    backup_state()
    clean_old_data(plan, cache)

    driver = build_driver()
    try:
        driver.get(CATEGORY_URL)
        time.sleep(2)
        dismiss_cookie_banner(driver)
        try:
            plan_for_scope(driver, FACET_PRIORITY, {}, plan, cache)
        except BlockedError as e:
            print(f"\n[STOPPED] {e}. LCSC is blocking this IP/session. Wait 30-60 min "
                  f"(or switch network), confirm lcsc.com opens normally, then re-run - "
                  f"progress is saved and will resume.")
            return
        print(f"\nDone. {len(plan['groups'])} groups saved to {PLAN_PATH}")
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
