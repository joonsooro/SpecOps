from __future__ import annotations

from pydantic import Field

from .contracts import WorkshopModel


class DemoOpenDecision(WorkshopModel):
    decision_id: str = Field(pattern=r"^D-[0-9]{2}$")
    title: str
    source_name: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)


CSV_EXPORT_OPEN_DECISION = DemoOpenDecision(
    decision_id="D-01",
    title="Canonical business date",
    source_name="DEV_LEAD_TECHNICAL_SPEC",
    line_start=525,
    line_end=526,
)

LIVE_PM_TURN = (
    "For the filtered-orders CSV export, preserve every captured filter and all matching rows. "
    "Please identify the next unresolved product decision from the Dev Lead evidence."
)
