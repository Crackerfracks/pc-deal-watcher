"""Rough value checks for deals, so the watcher can flag the great ones.

Prices here are typical US prices from mid-October 2026 research (see
plans/pc-build/builds.md in the project). They are estimates for comparing a
prebuilt against buying the same parts, not quotes. Update them as the market
moves.
"""

import re

# Graphics cards: (pattern in the squashed title, VRAM GB, typical price, NVIDIA?)
GPUS = [
    ("5090", 32, 2400, True),
    ("5080", 16, 1300, True),
    ("4090", 24, 2200, True),
    ("4080", 16, 1000, True),
    ("5070ti", 16, 1150, True),
    ("3090", 24, 1375, True),
    ("7900xtx", 24, 1100, False),
    ("7900xt", 20, 750, False),
    ("9070xt", 16, 790, False),
    ("9070gre", 12, 550, False),
    ("9070", 16, 650, False),
    ("5060ti16", 16, 730, True),
    ("4060ti16", 16, 870, True),
    ("9060xt16", 16, 500, False),
    ("5070", 12, 720, True),
    ("4070", 12, 650, True),
]

CPUS = [
    ("9950x3d", 700), ("9950x", 550), ("9900x3d", 550), ("9900x", 400),
    ("9800x3d", 444), ("7800x3d", 400), ("9700x", 300), ("7700x", 250),
    ("8700g", 260), ("8700f", 230), ("9600x", 200), ("7600x", 180), ("8400f", 130),
    ("14900", 450), ("14700", 330), ("14600", 260), ("14400", 150),
    ("ultra9", 500), ("ultra7", 300), ("ultra5", 220),
]
CPU_DEFAULT = 250

RAM_PER_GB_DDR5 = 14.0  # ~$449 for 32 GB
SSD_PRICE = {1: 120, 2: 210, 4: 380}
# Motherboard, power supply, case and cooler together.
PLATFORM_BASE = 365
# Stephen's budget is $1,500 to start; a prebuilt far past it isn't a great
# deal for him however much it saves.
PREBUILT_GREAT_MAX = 1800


def squash(text):
    return re.sub(r"[\s\-]+", "", text.lower())


def find_gpu(t):
    for key, vram, price, nvidia in GPUS:
        if key in t:
            return {"model": key, "vram": vram, "price": price, "nvidia": nvidia}
    return None


def judge_prebuilt(title, price):
    """Estimate what the same parts would cost and whether it suits the goals."""
    t = squash(title)
    gpu = find_gpu(t)
    cpu_price = next((p for k, p in CPUS if k in t), CPU_DEFAULT)
    # Read RAM from the unsquashed title so "GDDR7 32GB DDR5" isn't read as 732 GB.
    ram = re.search(r"\b(\d{2,3})\s?gb\s?(?:ddr5|ram|memory|ddr4)", title.lower())
    ram_gb = int(ram.group(1)) if ram else 16
    ssd = re.search(r"(\d)tb", t)
    ssd_tb = int(ssd.group(1)) if ssd else 1
    parts = (
        (gpu["price"] if gpu else 0)
        + cpu_price
        + ram_gb * RAM_PER_GB_DDR5
        + SSD_PRICE.get(ssd_tb, 120)
        + PLATFORM_BASE
    )
    notes = []
    suitable = True
    if not gpu:
        notes.append("couldn't identify the graphics card")
        suitable = False
    elif gpu["vram"] < 16:
        notes.append(f"only {gpu['vram']} GB of graphics memory, too little for local AI")
        suitable = False
    else:
        notes.append(f"{gpu['vram']} GB graphics card ({'NVIDIA, best for AI and Blender' if gpu['nvidia'] else 'AMD, fine for games, weaker for AI tools and Blender'})")
    if ram_gb < 32:
        notes.append(f"{ram_gb} GB RAM, add ~${(32 - ram_gb) * RAM_PER_GB_DDR5:.0f} to reach 32 GB")
    savings = parts - price
    return {
        "parts_value": round(parts),
        "savings": round(savings),
        "suitable": suitable,
        "notes": notes,
        "great": suitable and savings >= 250 and price <= PREBUILT_GREAT_MAX,
    }


# A great part deal must also save real money, so a $20 cooler can't set off
# urgent alerts.
GREAT_MIN_SAVING = 25


def is_great_part(part, price):
    """A part deal is 'great' when it's at least 10% and $25 under its target."""
    return (
        bool(part.get("builds"))
        and price <= part["max_price"] * 0.9
        and part["max_price"] - price >= GREAT_MIN_SAVING
    )
