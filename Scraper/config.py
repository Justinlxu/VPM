"""
Configuration — thresholds, scraping settings, VLR event IDs.
Edit this file to tune economy definitions or add events to scrape.
"""

# ══════════════════════════════════════════════════════════════════
# YOUR ECONOMY DEFINITIONS
# ══════════════════════════════════════════════════════════════════

# Eco round: loadout difference >= this value means the poorer team is on an eco
ECO_LOADOUT_DIFF = 12500

# Gun round: both teams have loadout >= this value
GUN_ROUND_LOADOUT = 16000

# Bonus: winning first 3 rounds of a half (pistol + buy + bonus)
# Halves start at round 1 and round 13
HALF_STARTS = [1, 13]
BONUS_STREAK_LENGTH = 3

# Maximum rounds to track per map
MAX_ROUNDS = 32

# ══════════════════════════════════════════════════════════════════
# SCRAPING SETTINGS
# ══════════════════════════════════════════════════════════════════

# Seconds between requests (be respectful to VLR.gg)
RATE_LIMIT = 2.0

# Max retries per page
MAX_RETRIES = 3

# Timeout per request (seconds)
REQUEST_TIMEOUT = 15

# User agent string
USER_AGENT = "ValorantPredictor/1.0 (esports research project)"

# Progress save frequency (save excel every N matches)
SAVE_EVERY = 25

# ══════════════════════════════════════════════════════════════════
# VLR.gg EVENT IDs TO SCRAPE
# ══════════════════════════════════════════════════════════════════
# Find event IDs from the URL: vlr.gg/event/{ID}/event-name
#
# Add or remove events here. The scraper processes them in order.
# Format: (event_id, event_name)

EVENTS = [
    # ── 2023 International ─────────────────────────────────────
    (1188, "VCT 2023 LOCK//IN Sao Paulo",           ),
    (1494, "VCT 2023 Masters Tokyo",                ),
    (1657, "VCT 2023 Champions Los Angeles",        ),

    # ── 2023 Leagues ──
    (1189, "VCT 2023 Americas League"              ),
    (1190, "VCT 2023 EMEA League"                  ),
    (1191, "VCT 2023 Pacific League"               ),
    (1664, "VCT 2023 Champions China Qualifier"    ),
    (1658, "VCT 2023 Americas LCQ"                 ),
    (1659, "VCT 2023 EMEA LCQ"                     ),
    (1660, "VCT 2023 Pacific LCQ"                  ),

    # ── 2024 International ─────────────────────────────────────
    (1921, "VCT 2024 Masters Madrid"               ),
    (1999, "VCT 2024 Masters Shanghai"             ),
    (2097, "VCT 2024 Champions Seoul"              ),

    # ── 2024 Leagues ──
    (2004, "VCT 2024 Americas Stage 1"             ),
    (1998, "VCT 2024 EMEA Stage 1"                 ),
    (2002, "VCT 2024 Pacific Stage 1"              ),
    (2006, "VCT 2024 China Stage 1"                ),
    (2095, "VCT 2024 Americas Stage 2"             ),
    (2094, "VCT 2024 EMEA Stage 2"                 ),
    (2005, "VCT 2024 Pacific Stage 2"              ),
    (2096, "VCT 2024 China Stage 2"                ),

    # ── 2025 International ─────────────────────────────────────
    (2281, "VCT 2025 Masters Bangkok"              ),
    (2282, "VCT 2025 Masters Toronto"              ),
    (2283, "VCT 2025 Champions Seoul"              ),

    # ── 2025 Leagues ──
    (2347, "VCT 2025 Americas Stage 1"             ),
    (2380, "VCT 2025 EMEA Stage 1"                 ),
    (2379, "VCT 2025 Pacific Stage 1"              ),
    (2359, "VCT 2025 China Stage 1"                ),
    (2501, "VCT 2025 Americas Stage 2"             ),
    (2498, "VCT 2025 EMEA Stage 2"                 ),
    (2500, "VCT 2025 Pacific Stage 2"              ),
    (2499, "VCT 2025 China Stage 2"                ),

    # ── 2026 Kickoffs (your training data) ─────────────────────
    (2682, "VCT 2026 Kickoff Americas"             ),
    (2684, "VCT 2026 Kickoff EMEA"                 ),
    (2683, "VCT 2026 Kickoff Pacific"              ),
    (2685, "VCT 2026 Kickoff China"                ),

    # ── Masters Santiago (your TEST data) ──────────────────────
    # (2760, "Masters Santiago 2026",               ),
    # Uncomment when you want to scrape Santiago results
]

# ══════════════════════════════════════════════════════════════════
# OUTPUT
# ══════════════════════════════════════════════════════════════════

OUTPUT_FILE = "valorant_data.xlsx"
PROGRESS_FILE = "scrape_progress.json"
