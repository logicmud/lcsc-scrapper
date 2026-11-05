"""
LCSC Phase 2 - Group Scraper
-----------------------------
Reads the _group_plan.json produced by the Phase 1 planner and, for every
group (each guaranteed <=5000 results), visits the group's Copy-Link URL,
sets page size to 100, and clicks "Download Table" once per page - reusing
the same logic as the working <5000-result script.

Resumable: skips any page whose output file already exists, and skips any
group whose "_done.flag" marker file already exists.

Requirements:
    pip install selenium
"""

import os
import time
import glob
import math
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import (
    TimeoutException, NoSuchElementException, ElementClickInterceptedException,
    ElementNotInteractableException, StaleElementReferenceException,
)
import json
import sys

# Run with:  python lcsc_phase2_scraper_ceramic_cap.py --fresh-only   (pass 1: skip expired links)
#       then:  python lcsc_phase2_scraper_ceramic_cap.py                (pass 2: rebuild expired ones)
FRESH_ONLY = "--fresh-only" in sys.argv
# Default: ignore saved links and always rebuild each group from its saved filters.
# Pass --use-saved-links to try the saved Copy-Link URL first (only useful if links are <72h old).
USE_SAVED_LINKS = "--use-saved-links" in sys.argv or FRESH_ONLY

class LinkExpiredSkip(Exception):
    pass

# Reuse the planner's filter logic so an expired Copy-Link URL can be rebuilt
# from the group's saved filters (run this script from the same folder as the planner).
import lcsc_phase1_planner_ceramic_cap as planner


def link_expired(driver):
    try:
        txt = driver.find_element(By.TAG_NAME, "body").text
    except Exception:
        return False
    return "Link Expired" in txt


def open_group(driver, group):
    """Open the saved Copy-Link URL. LCSC links die after 72h, so if it has
    expired, re-apply the group's saved filters on the category page instead."""
    if USE_SAVED_LINKS:
        driver.get(group["url"])
        time.sleep(2)
        if not link_expired(driver):
            return
        if FRESH_ONLY:
            raise LinkExpiredSkip()
        print("  [expired] saved link is older than 72h - rebuilding from saved filters...")
    else:
        print("  [rebuild] applying saved filters on category page...")
    try:
        driver.get(planner.CATEGORY_URL)
    except TimeoutException:
        pass
    time.sleep(3)
    planner.dismiss_cookie_banner(driver)
    planner.apply_filters(driver, group["filters"])
    time.sleep(2)


def click_with_retry(driver, find_func, max_attempts=4):
    """Locate an element via find_func() and click it. If the element goes
    stale between locating and clicking (common on this SPA after many
    page transitions), re-locate fresh and try again."""
    last_exc = None
    for attempt in range(max_attempts):
        try:
            el = find_func()
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
            try:
                el.click()
            except (ElementClickInterceptedException, ElementNotInteractableException):
                driver.execute_script("arguments[0].click();", el)
            return
        except StaleElementReferenceException as e:
            last_exc = e
            time.sleep(0.5)
            continue
    raise last_exc

# ----------------------------- CONFIG ---------------------------------

BASE_DIR = r"C:\Users\ruchi\OneDrive\Desktop\DIACSIDAC\Ceramic Capacitors"
PLAN_PATH = os.path.join(BASE_DIR, "_group_plan.json")
STATE_DIR = os.path.join(BASE_DIR, "_state")      # done-flags, tmp downloads, debug dumps
TMP_DIR = os.path.join(STATE_DIR, "_tmp_downloads")
# ALL downloaded xlsx files go directly into BASE_DIR (one flat folder).

PAGE_SIZE = "100"
DOWNLOAD_WAIT_TIMEOUT = 60
PAGE_LOAD_TIMEOUT = 20
CHROME_PROFILE_DIR = r"C:\Users\ruchi\ChromeSeleniumProfile"  # persistent profile - reuse the same one you used in Phase 1

# ------------------------------------------------------------------------


def safe_folder_name(desc: str) -> str:
    keep = "".join(c if c.isalnum() or c in " _-+=." else "_" for c in desc)
    return keep[:150].strip() or "group"


def build_driver(download_dir):
    options = Options()
    options.add_argument("--start-maximized")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    if CHROME_PROFILE_DIR:
        options.add_argument(f"--user-data-dir={CHROME_PROFILE_DIR}")
    prefs = {
        "download.default_directory": download_dir,
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "safebrowsing.enabled": True,
    }
    options.add_experimental_option("prefs", prefs)
    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    })

    # Belt-and-suspenders: some Chrome installs override the "prefs" download
    # settings above and still show a native "Save As" dialog, which Selenium
    # cannot interact with (it just hangs until the download-wait times out).
    # Setting this via CDP forces silent, fixed-location downloads regardless
    # of the machine's own Chrome settings.
    try:
        driver.execute_cdp_cmd("Page.setDownloadBehavior", {
            "behavior": "allow",
            "downloadPath": download_dir,
        })
    except Exception as e:
        print(f"[warn] Could not set CDP download behavior ({e}); "
              f"relying on prefs only - a native Save As dialog may still appear.")

    return driver


def wait_for_new_download(download_dir, before_files, timeout=DOWNLOAD_WAIT_TIMEOUT):
    end_time = time.time() + timeout
    while time.time() < end_time:
        current = set(glob.glob(os.path.join(download_dir, "*")))
        new_files = {f for f in current - before_files
                     if not f.endswith(".crdownload") and not f.endswith(".tmp")}
        for f in list(new_files):
            try:
                s1 = os.path.getsize(f)
                time.sleep(1)
                s2 = os.path.getsize(f)
            except FileNotFoundError:
                continue
            if s1 == s2:
                return f
        time.sleep(0.5)
    raise TimeoutException("Timed out waiting for a new download to appear.")


def dump_debug(driver, out_dir, label):
    try:
        driver.save_screenshot(os.path.join(out_dir, f"debug_{label}.png"))
        with open(os.path.join(out_dir, f"debug_{label}.html"), "w", encoding="utf-8") as f:
            f.write(driver.page_source)
    except Exception as e:
        print(f"[debug] could not save debug info: {e}")


def set_page_size_to_100(driver, wait, out_dir):
    trigger_xpaths = [
        "//div[contains(@class,'productPaginationWrap')]//div[@role='button' and @aria-haspopup='true']",
        "//div[contains(@class,'productPaginationWrap')]//div[@role='button']",
        "//div[contains(text(),'Showing')]/following-sibling::div[@role='button'][1]",
    ]
    trigger = None
    for xp in trigger_xpaths:
        try:
            trigger = WebDriverWait(driver, 5).until(EC.element_to_be_clickable((By.XPATH, xp)))
            break
        except TimeoutException:
            continue
    if trigger is None:
        print("[warn] page-size dropdown not found; continuing with default size.")
        dump_debug(driver, out_dir, "pagesize_trigger_not_found")
        return

    if trigger.tag_name.lower() == "select":
        Select(trigger).select_by_visible_text("100")
        time.sleep(2)
        return

    try:
        trigger.click()
    except ElementClickInterceptedException:
        driver.execute_script("arguments[0].click();", trigger)

    option_xpaths = [
        "//div[contains(@class,'v-list-item__title') and normalize-space(text())='100']",
        "//li[normalize-space(text())='100']",
        "//*[normalize-space(text())='100']",
    ]
    option = None
    for xp in option_xpaths:
        try:
            option = WebDriverWait(driver, 5).until(EC.element_to_be_clickable((By.XPATH, xp)))
            break
        except TimeoutException:
            continue
    if option is None:
        print("[warn] '100' option not found.")
        dump_debug(driver, out_dir, "pagesize_option_not_found")
        return
    try:
        option.click()
    except ElementClickInterceptedException:
        driver.execute_script("arguments[0].click();", option)
    time.sleep(2)


def click_download_table(driver, wait, download_dir):
    before = set(glob.glob(os.path.join(download_dir, "*")))

    def find_btn():
        return wait.until(EC.element_to_be_clickable(
            (By.XPATH, "//*[self::a or self::button][contains(., 'Download Table')]")))

    click_with_retry(driver, find_btn)
    return wait_for_new_download(download_dir, before)


def get_first_row_signature(driver):
    try:
        return driver.find_element(By.XPATH, "(//table//tr[position()>1])[1]").text
    except (NoSuchElementException, StaleElementReferenceException):
        return None


def go_to_next_page(driver, wait, current_page, out_dir):
    before_sig = get_first_row_signature(driver)

    def find_btn():
        icons = wait.until(EC.presence_of_all_elements_located(
            (By.XPATH, "//i[contains(@class,'mdi-chevron-right')]")))
        icon = icons[-1]
        try:
            return icon.find_element(By.XPATH, "./ancestor::button[1]")
        except NoSuchElementException:
            return icon

    click_with_retry(driver, find_btn)
    try:
        WebDriverWait(driver, 20).until(
            lambda d: get_first_row_signature(d) not in (None, before_sig))
    except TimeoutException:
        print(f"[warn] page {current_page + 1}: table content looked unchanged, continuing.")
        dump_debug(driver, out_dir, f"page{current_page + 1}_not_confirmed")


def close_extra_tabs(driver):
    """Keep exactly one tab open; close any extra tabs/windows LCSC or Chrome opened."""
    try:
        handles = driver.window_handles
        if len(handles) > 1:
            for h in handles[1:]:
                driver.switch_to.window(h)
                driver.close()
            driver.switch_to.window(handles[0])
    except Exception:
        pass


def scrape_group(driver, group_idx, group):
    desc = group["description"]
    count = group["count"]
    url = group["url"]
    total_pages = max(1, math.ceil(count / 100))

    os.makedirs(BASE_DIR, exist_ok=True)
    os.makedirs(TMP_DIR, exist_ok=True)
    done_flag = os.path.join(STATE_DIR, f"group_{group_idx:03d}_done.flag")
    prefix = f"{group_idx:03d}_{safe_folder_name(desc)[:80]}"

    def page_file(page, ext=".xlsx"):
        # Flat naming: <group#>_<manufacturer(s)>_page_<n>.xlsx, all in BASE_DIR
        return os.path.join(BASE_DIR, f"{prefix}_page_{page:03d}{ext}")

    if os.path.exists(done_flag):
        print(f"[skip] already completed: {desc}")
        return

    print(f"\n=== Group {group_idx}: {desc} ({count} results, {total_pages} pages) ===")

    close_extra_tabs(driver)
    wait = WebDriverWait(driver, 20)
    try:
        open_group(driver, group)
        set_page_size_to_100(driver, wait, STATE_DIR)
        time.sleep(1)

        try:
            WebDriverWait(driver, 30).until(
                lambda d: get_first_row_signature(d) is not None)
        except TimeoutException:
            print("  [warn] table didn't show rows within 30s; continuing anyway.")
            dump_debug(driver, STATE_DIR, f"g{group_idx:03d}_initial_table_not_loaded")

        for page in range(1, total_pages + 1):
            if os.path.exists(page_file(page)):
                print(f"  [skip] page {page}/{total_pages} already downloaded")
                if page != total_pages:
                    go_to_next_page(driver, wait, page, STATE_DIR)
                    time.sleep(1.5)
                continue

            print(f"  [page {page}/{total_pages}] downloading...")
            downloaded = click_download_table(driver, wait, TMP_DIR)
            ext = os.path.splitext(downloaded)[1] or ".xlsx"
            final_name = page_file(page, ext)
            os.replace(downloaded, final_name)
            print(f"    saved: {final_name}")

            if page != total_pages:
                time.sleep(2)  # let any post-download toast/snackbar clear
                go_to_next_page(driver, wait, page, STATE_DIR)
                time.sleep(1.5)

        with open(done_flag, "w") as f:
            f.write("done\n")
        print(f"  [done] group complete: {desc}")

    except LinkExpiredSkip:
        print("  [skip-expired] link expired; will be handled in the normal pass.")


def main():
    with open(PLAN_PATH, "r", encoding="utf-8") as f:
        plan = json.load(f)

    groups = plan["groups"]
    print(f"Loaded {len(groups)} groups from {PLAN_PATH}")

    os.makedirs(TMP_DIR, exist_ok=True)
    driver = build_driver(TMP_DIR)
    since_restart = 0
    try:
        for idx, group in enumerate(groups, start=1):
            if os.path.exists(os.path.join(STATE_DIR, f"group_{idx:03d}_done.flag")):
                print(f"[skip] already completed: {group['description']}")
                continue
            try:
                scrape_group(driver, idx, group)
                since_restart += 1
            except Exception as e:
                print(f"[ERROR] group '{group['description']}' failed: {e}")
                print("        Re-run this script later - completed groups/pages are skipped automatically.")
                # browser may be in a bad state: restart it
                try:
                    driver.quit()
                except Exception:
                    pass
                driver = build_driver(TMP_DIR)
                since_restart = 0
            if since_restart >= 25:   # fresh browser every 25 groups to avoid memory build-up
                try:
                    driver.quit()
                except Exception:
                    pass
                driver = build_driver(TMP_DIR)
                since_restart = 0
    finally:
        try:
            driver.quit()
        except Exception:
            pass

    print("\nAll groups processed.")


if __name__ == "__main__":
    main()
