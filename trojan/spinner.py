"""The words and frames shown while the model is thinking."""
from __future__ import annotations

import random
from typing import Optional

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

VERBS = [
    # the house favourites
    "Flibbertigibbeting", "Percolating", "Befuddling", "Booping", "Ruminating",
    "Cogitating", "Galumphing", "Skedaddling", "Bamboozling", "Shenaniganizing",
    # and a few from inside the horse
    "Galloping", "Trotting", "Whinnying", "Besieging", "Hoofing it", "Sneaking past the walls",
    "Unbolting the hatch", "Rattling the wheels", "Counting the soldiers", "Plotting in the belly",
]

VERB_SECONDS = 2.5  # how long each word stays before the next one


def next_verb(current: Optional[str] = None) -> str:
    choices = [v for v in VERBS if v != current]
    return random.choice(choices)


def frame(tick: int) -> str:
    return SPINNER[tick % len(SPINNER)]
