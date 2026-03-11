"""
VLR.gg → Excel Scraper — Main Entry Point

Scrapes VCT match data from VLR.gg and writes to a 3-sheet Excel file.

Usage:
    python main.py                        # Scrape all events in config.py
    python main.py --event 2700           # Scrape a single event by ID
    python main.py --match 440364         # Scrape a single match by ID
    python main.py --match 440364 --game 2  # Scrape only map 2 from a match
    python main.py --resume               # Resume interrupted scrape
    python main.py --reset                # Delete progress and start fresh
    python main.py --inspect              # Show what loadout selectors look like
"""

import argparse
import os
import time
from config import EVENTS, OUTPUT_FILE, PROGRESS_FILE, SAVE_EVERY
from scraper import VLRScraper
from excel_writer import (
    load_or_create_workbook, write_match_data,
    save_workbook, print_summary, derive_features_from_rounds
)


def scrape_all():
    """Scrape all events defined in config.py."""
    scraper = VLRScraper()
    wb = load_or_create_workbook()
    total_maps = 0
    start_time = time.time()

    for event_id, event_name in EVENTS:
        maps = scraper.scrape_event(event_id, event_name)

        for i, map_data in enumerate(maps):
            write_match_data(wb, map_data)
            total_maps += 1

            # Periodic save
            if total_maps % SAVE_EVERY == 0:
                save_workbook(wb)
                elapsed = time.time() - start_time
                print(f"\n  💾 Saved — {total_maps} maps total ({elapsed/60:.1f} min elapsed)")

        # Save after each event
        save_workbook(wb)

    # Derive Bonus / Eco / Gun columns from the Round Data sheet
    print("\n  Computing economy features from round data...")
    derive_features_from_rounds(wb)
    save_workbook(wb)

    print_summary(wb)
    elapsed = time.time() - start_time
    print(f"\n  Completed in {elapsed/60:.1f} minutes")
    print(f"  Total maps scraped: {total_maps}")


def scrape_single_match(match_id, game_id=None):
    """Scrape a single match by ID, optionally filtered to one game/map."""
    scraper = VLRScraper()
    wb = load_or_create_workbook()

    if str(match_id) in scraper.scraped_ids:
        print(f"\n  Match {match_id} already fully scraped — skipping")
        return

    match_url = f"{VLRScraper.BASE_URL}/{match_id}"
    if game_id is not None:
        print(f"\n  Scraping match {match_id}, game {game_id}...")
    else:
        print(f"\n  Scraping match {match_id} (all maps)...")

    maps = scraper.scrape_match(match_url, match_id, event_name="", game_filter=game_id)
    for map_data in maps:
        write_match_data(wb, map_data)

    if game_id is None:
        scraper._mark_done(match_id)

    print("\n  Computing economy features from round data...")
    derive_features_from_rounds(wb)
    save_workbook(wb)
    print_summary(wb)


def scrape_single(event_id):
    """Scrape a single event by ID."""
    # Look up event name from config, or use defaults
    event_name = ""
    for eid, ename in EVENTS:
        if eid == event_id:
            event_name = ename
            break

    if not event_name:
        event_name = f"Event {event_id}"
        print(f"  Event {event_id} not in config.py — using default name")

    scraper = VLRScraper()
    wb = load_or_create_workbook()

    maps = scraper.scrape_event(event_id, event_name)
    for map_data in maps:
        write_match_data(wb, map_data)

    # Derive Bonus / Eco / Gun columns from the Round Data sheet
    print("\n  Computing economy features from round data...")
    derive_features_from_rounds(wb)
    save_workbook(wb)
    print_summary(wb)


def inspect_page():
    """
    Fetch a single VLR match page and print the HTML structure
    of the economy section. Use this to debug/fix CSS selectors.
    """
    from bs4 import BeautifulSoup
    import requests

    print("\n  This tool helps you inspect VLR.gg's HTML structure")
    print("  to verify or fix the scraper's CSS selectors.\n")

    url = input("  Paste a VLR.gg match URL: ").strip()
    if not url:
        url = "https://www.vlr.gg/440364"  # Example match
        print(f"  Using example: {url}")

    resp = requests.get(url, headers={"User-Agent": "ValorantScraper/1.0"})
    soup = BeautifulSoup(resp.text, "lxml")

    # Show economy section structure
    print("\n  ── ECONOMY SECTION HTML STRUCTURE ──\n")

    econ_sections = soup.select(".vm-stats-game")
    for sec in econ_sections:
        game_id = sec.get("data-game-id", "?")
        if game_id == "all":
            continue

        print(f"  Game ID: {game_id}")

        # Look for any elements with round data
        round_els = sec.select("[class*='round']")
        print(f"  Elements with 'round' in class: {len(round_els)}")
        for el in round_els[:3]:
            print(f"    Tag: {el.name}, Classes: {el.get('class')}")
            print(f"    Data attrs: {dict((k,v) for k,v in el.attrs.items() if k.startswith('data-'))}")

        # Look for loadout data
        loadout_els = sec.select("[data-loadout]")
        print(f"  Elements with data-loadout: {len(loadout_els)}")
        for el in loadout_els[:3]:
            print(f"    Loadout value: {el.get('data-loadout')}")

        # Look for bank/economy text
        bank_els = sec.select("[class*='bank'], [class*='econ']")
        print(f"  Elements with 'bank' or 'econ' in class: {len(bank_els)}")
        for el in bank_els[:5]:
            print(f"    Tag: {el.name}, Classes: {el.get('class')}, Text: '{el.text.strip()[:50]}'")

        # Show first few round indicator elements
        print(f"\n  Round indicator elements:")
        for cls in [".vlr-rounds-row-col", ".rnd-sq", ".mod-win", ".mod-loss"]:
            found = sec.select(cls)
            if found:
                print(f"    {cls}: {len(found)} elements")
                for el in found[:2]:
                    print(f"      Classes: {el.get('class')}")
                    print(f"      Data: {dict((k,v) for k,v in el.attrs.items() if k.startswith('data-'))}")

        print()
        break  # Just show first map


def derive_only():
    """Re-derive Bonus/Eco/Gun from existing Round Data without re-scraping."""
    if not os.path.exists(OUTPUT_FILE):
        print(f"  No workbook found at {OUTPUT_FILE}. Run a scrape first.")
        return
    from openpyxl import load_workbook
    wb = load_workbook(OUTPUT_FILE)
    print(f"  Loaded {OUTPUT_FILE}")
    print("  Computing economy features from round data...")
    derive_features_from_rounds(wb)
    wb.save(OUTPUT_FILE)
    print(f"  Saved {OUTPUT_FILE}")


def reset():
    """Delete progress file and output to start fresh."""
    for f in [PROGRESS_FILE, OUTPUT_FILE]:
        if os.path.exists(f):
            os.remove(f)
            print(f"  Deleted {f}")
    print("  Ready for a fresh scrape")


def main():
    parser = argparse.ArgumentParser(description="Scrape VLR.gg → Excel")
    parser.add_argument("--event",  type=int, help="Scrape a single event by ID")
    parser.add_argument("--match",  type=int, nargs="+", help="Scrape one or more matches by ID")
    parser.add_argument("--game",   type=int, help="With --match: only scrape this game/map number (1, 2, 3...)")
    parser.add_argument("--resume", action="store_true", help="Resume interrupted scrape")
    parser.add_argument("--reset",  action="store_true", help="Delete progress, start fresh")
    parser.add_argument("--inspect", action="store_true", help="Inspect VLR HTML structure")
    parser.add_argument("--derive", action="store_true", help="Re-derive economy features from existing Round Data")
    args = parser.parse_args()

    if args.reset:
        reset()
    elif args.inspect:
        inspect_page()
    elif args.derive:
        derive_only()
    elif args.match:
        for mid in args.match:
            scrape_single_match(mid, game_id=args.game)
    elif args.event:
        scrape_single(args.event)
    else:
        scrape_all()


if __name__ == "__main__":
    main()
