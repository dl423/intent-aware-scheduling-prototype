from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass(frozen=True)
class WorkloadItem:
    id: str
    persona: str
    request_text: str
    arrival_hour: float
    deadline_hours: float
    quality: str
    max_cost_usd: float
    attention: str
    nudge_after_hours: float | None = None


PERSONA_TEXT = {
    "urgent-executive": (
        "Prepare a board-ready incident response memo. This is critical and I am "
        "waiting now. Make the recommendation defensible without asking me questions."
    ),
    "overnight-researcher": (
        "Produce a high-quality research synthesis by tomorrow morning for less than "
        "$2. Batch any necessary questions into one checkpoint."
    ),
    "casual-drafter": (
        "Give me a rough first draft when capacity is available. Keep the cost very low."
    ),
    "interactive-collaborator": (
        "Help me refine this proposal while I am here. Ask when a decision changes the "
        "direction, and keep me updated."
    ),
}


DEFAULT_BURSTS = 8

# Each row gives the arrival time in minutes after 09:00 and the numbers of
# interactive collaborators and patient drafts. Every burst also includes
# one urgent executive and one overnight researcher. Varying the burst size
# and spacing creates busy periods and recovery gaps without random arrivals.
BURST_PROFILE = (
    (0, 2, 3),
    (40, 5, 5),
    (100, 4, 6),
    (200, 2, 2),
    (230, 5, 6),
    (280, 3, 5),
    (410, 1, 3),
    (470, 2, 2),
)


def generate_workload(bursts: int = DEFAULT_BURSTS) -> list[WorkloadItem]:
    """Generate a fixed mix of tasks that compete for execution capacity.

    The default workload contains 32 human-waiting tasks and 40 patient tasks
    in eight bursts of different sizes and intervals. Smaller runs use the
    first bursts of this profile, and longer runs repeat it every ten hours.
    Relative deadlines follow the same rules for all run sizes, and changing
    the number of bursts does not change the service-duration assumptions.
    The paper reports only the default eight-burst workload.
    """

    if bursts < 1:
        raise ValueError("bursts must be positive")
    items: list[WorkloadItem] = []
    sequence = 0
    collaborator_count = 0
    draft_count = 0
    for burst in range(bursts):
        cycle, index = divmod(burst, len(BURST_PROFILE))
        minutes, collaborators, drafts = BURST_PROFILE[index]
        arrival = 9.0 + 10 * cycle + minutes / 60
        definitions = [
            ("urgent-executive", 0.5, "critical", 8.0, "do-not-interrupt"),
            *[
                (
                    "interactive-collaborator",
                    2.0 + 0.5 * ((collaborator_count + i) % 3),
                    "standard", 1.2, "ask-freely",
                )
                for i in range(collaborators)
            ],
            ("overnight-researcher", 18.0, "high", 2.0, "batch-questions"),
            *[
                (
                    "casual-drafter", 18.0 + (draft_count + i) % 4,
                    "draft", 0.20, "do-not-interrupt",
                )
                for i in range(drafts)
            ],
        ]
        # Cycle deadlines across each persona's full stream, not per burst,
        # preserving the original relative-deadline distribution.
        collaborator_count += collaborators
        draft_count += drafts
        for persona, deadline, quality, cap, attention in definitions:
            sequence += 1
            items.append(
                WorkloadItem(
                    id=f"sim-{sequence:03d}",
                    persona=persona,
                    request_text=PERSONA_TEXT[persona],
                    arrival_hour=arrival,
                    deadline_hours=deadline,
                    quality=quality,
                    max_cost_usd=cap,
                    attention=attention,
                )
            )
    return items


def absolute_times(item: WorkloadItem, start: datetime | None = None) -> tuple[datetime, datetime]:
    start = start or datetime(2026, 7, 12, tzinfo=timezone.utc)
    arrival = start + timedelta(hours=item.arrival_hour)
    return arrival, arrival + timedelta(hours=item.deadline_hours)
