"""Level utilities: text rendering, generating good levels, and adding controlled defects.

All levels are for the PCG Benchmark problem "zelda-v0" (7 rows x 11 columns).
Tile codes used by the benchmark: 0 wall, 1 floor, 2 player, 3 key, 4 door, 5 enemy.
"""
import numpy as np
import pcg_benchmark

PROBLEM = "zelda-v0"
WALL, FLOOR, PLAYER, KEY, DOOR, ENEMY = range(6)
CHARS = {WALL: "#", FLOOR: ".", PLAYER: "@", KEY: "K", DOOR: "D", ENEMY: "E"}
CODES = {c: v for v, c in CHARS.items()}

# Info fields reported as ground truth (all computed by the benchmark itself).
INFO_FIELDS = ["regions", "players", "keys", "doors", "enemies", "player_key", "key_door"]


def make_env():
    return pcg_benchmark.make(PROBLEM)


def to_text(level):
    return "\n".join("".join(CHARS[int(v)] for v in row) for row in np.asarray(level))


def from_text(text):
    return np.array([[CODES[c] for c in line] for line in text.strip().splitlines()])


def evaluate(env, level):
    """Return (quality score in [0, 1], dict of benchmark facts) for one level."""
    _, quality, info = env.quality(np.asarray(level).tolist())
    facts = {k: int(info[k]) for k in INFO_FIELDS}
    return float(quality), facts


def hill_climb(env, rng, max_steps=6000):
    """Mutate 1-2 tiles at a time, keep changes that do not lower quality, stop at 1.0.

    Returns a level with quality 1.0, or None if the budget runs out.
    """
    level = np.array(env.content_space.sample())
    quality, _ = evaluate(env, level)
    for _ in range(max_steps):
        cand = level.copy()
        for _ in range(rng.integers(1, 3)):
            y, x = rng.integers(cand.shape[0]), rng.integers(cand.shape[1])
            cand[y, x] = rng.integers(6)
        cq, _ = evaluate(env, cand)
        if cq >= quality:
            level, quality = cand, cq
        if quality >= 1.0:
            return level
    return None


# ---------------------------------------------------------------------------
# Controlled defects. Each takes a perfect level and breaks exactly one rule.
# Each returns a new level, or None if it cannot be applied to this level.
# ---------------------------------------------------------------------------

def _neighbours(level, y, x):
    h, w = level.shape
    for dy, dx in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
        ny, nx = y + dy, x + dx
        if 0 <= ny < h and 0 <= nx < w:
            yield ny, nx


def defect_no_key(level, rng):
    """Rule broken: there must be exactly one key. The key becomes floor."""
    out = level.copy()
    out[out == KEY] = FLOOR
    return out


def defect_extra_player(level, rng):
    """Rule broken: there must be exactly one player. A second player is placed on a floor tile."""
    out = level.copy()
    floor = np.argwhere(out == FLOOR)
    if len(floor) == 0:
        return None
    y, x = floor[rng.integers(len(floor))]
    out[y, x] = PLAYER
    return out


def defect_door_blocked(level, rng):
    """Rule broken: the door must be reachable. Every open tile next to the door becomes wall."""
    out = level.copy()
    (y, x), = np.argwhere(out == DOOR)
    for ny, nx in _neighbours(out, y, x):
        if out[ny, nx] != WALL:
            if out[ny, nx] in (PLAYER, KEY):
                return None  # would delete a required object; skip this level
            out[ny, nx] = WALL
    return out


def defect_no_enemies(level, rng):
    """Rule broken (mild): there must be 2-4 enemies. All enemies become floor."""
    out = level.copy()
    out[out == ENEMY] = FLOOR
    return out


DEFECTS = {
    "no_key": defect_no_key,
    "extra_player": defect_extra_player,
    "door_blocked": defect_door_blocked,
    "no_enemies": defect_no_enemies,
}

# What must be true of the benchmark facts after each defect (manipulation check).
CHECKS = {
    "none": lambda f, q: q >= 1.0,
    "no_key": lambda f, q: f["keys"] == 0 and q < 1.0,
    "extra_player": lambda f, q: f["players"] == 2 and q < 1.0,
    # door unreachable, but the key must stay reachable and the enemy count must stay valid
    "door_blocked": lambda f, q: (f["key_door"] < 0 and f["doors"] == 1 and f["player_key"] > 0
                                  and 2 <= f["enemies"] <= 4 and q < 1.0),
    "no_enemies": lambda f, q: f["enemies"] == 0 and q < 1.0,
}
