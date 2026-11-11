"""Data shapes shared across the radar."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Board:
    """One company job board on one applicant-tracking system (ATS)."""

    ats: str          # greenhouse | lever | ashby | smartrecruiters | workday | auto
    board: str        # slug; for workday "tenant|wdN|site"; may start with "eu:" for EU-hosted boards
    company: str
    priority: bool = False      # from companies_custom.csv: score boost
    cap_exempt: bool = False    # university / academic medical / nonprofit research (likely H-1B cap-exempt)
    named: bool = True          # False when `company` is only a guess from the board id (discovered boards)
    tier: str = "core"          # core (curated list) | found (discovered automatically)
    focus: bool = False         # a profile's own employer list: scanned in depth every run

    @property
    def key(self) -> str:
        return f"{self.ats}:{self.board}"


@dataclass
class Job:
    ats: str
    board: str
    job_id: str
    company: str
    title: str
    location: str = ""
    url: str = ""
    posted_at: datetime | None = None      # timezone-aware UTC when known
    workplace: str = ""                    # remote | hybrid | onsite | ""
    countries: list[str] = field(default_factory=list)  # country names/codes if the ATS gives them
    description: str = ""                  # plain text when available
    needs_detail: bool = False             # fetch the single-job endpoint for description/country/date
    detail_ref: str = ""                   # ATS-specific pointer for the detail call
    title_guess: bool = False              # title read from a URL slug; the detail page gives the real one
    company_guess: bool = False            # company is a guess; the detail page may name it
    found_by: str = ""                     # keyword whose full-text search of the board returned this job
    detail_tried: bool = False             # the single-job endpoint was asked this run (it may have failed)

    @property
    def uid(self) -> str:
        return f"{self.ats}:{self.board}:{self.job_id}"
