"""Read-only policy contracts for future optimization engines."""
from dataclasses import dataclass
from typing import Protocol
import numpy as np


@dataclass(frozen=True)
class Objective:
    organic_views: float = 1.0
    subscribers: float = 0.0
    revenue: float = 0.0
    viewer_value: float = 0.0


class ExperimentPolicy(Protocol):
    def propose(self, candidates: list[dict], observations: list[dict], objective: Objective) -> dict: ...


class BayesianOptimizer(Protocol):
    def suggest(self, search_space: dict, observations: list[dict], objective: Objective) -> dict: ...


def thompson_proposal(arms: dict[str, tuple[int, int]], seed: int = 42) -> dict:
    """Offline suggestion only. No assignment, publishing, or automatic channel edits.
    Counts must be independent, comparable preregistered experiment outcomes.
    """
    if not arms or any(w < 0 or f < 0 for w, f in arms.values()):
        raise ValueError("Nonnegative outcome counts and at least one arm required.")
    rng = np.random.default_rng(seed)
    draws = {key: float(rng.beta(1+w, 1+f)) for key, (w, f) in arms.items()}
    return {"suggested_arm": max(draws, key=draws.get), "posterior_draws": draws,
            "requires_human_approval": True, "status": "proposal_only"}
