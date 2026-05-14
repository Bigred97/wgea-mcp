"""Fuzzy search across the curated WGEA dataset registry."""
from __future__ import annotations

from rapidfuzz import fuzz, process

from . import curated as curated_mod
from .models import DatasetSummary


def list_summaries() -> list[DatasetSummary]:
    out: list[DatasetSummary] = []
    for cd in curated_mod.list_all():
        out.append(
            DatasetSummary(
                id=cd.id,
                name=cd.name,
                description=cd.description,
                update_frequency=cd.update_frequency,
                is_curated=True,
            )
        )
    return out


def search(query: str, limit: int = 10) -> list[DatasetSummary]:
    """Fuzzy-search curated datasets by id, name, description, and search_keywords."""
    if not query.strip():
        raise ValueError(
            "query is required. Try 'workforce', 'pay gap', 'parental leave', "
            "'harassment', 'flexible work', or any other WGEA topic."
        )
    summaries = list_summaries()
    if not summaries:
        return []
    keyword_lookup = {cd.id: " ".join(cd.search_keywords) for cd in curated_mod.list_all()}
    haystack = {
        i: f"{s.id} {s.name} {s.description or ''} {keyword_lookup.get(s.id, '')}"
        for i, s in enumerate(summaries)
    }
    matches = process.extract(
        query, haystack, scorer=fuzz.WRatio, limit=max(limit, len(summaries))
    )
    ordered = sorted(matches, key=lambda m: -m[1])
    return [summaries[idx] for _hay, _score, idx in ordered[:limit]]
