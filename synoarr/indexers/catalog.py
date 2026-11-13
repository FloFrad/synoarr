"""Built-in catalogue of indexers that can be added in one click.

ONLY legal sources belong here. Users can add any other source themselves by URL:
a Torznab endpoint (e.g. their own Jackett / Prowlarr compatible instance, with its
API key) or a plain RSS/Atom torrent feed - see indexers.detect_type().
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

CATALOG: List[Dict[str, Any]] = [
    {
        "id": "internet_archive",
        "name": "Internet Archive",
        "type": "internet_archive",
        "url": "https://archive.org",
        "description": "Films et séries du domaine public ou sous licence libre hébergés par archive.org "
                       "(téléchargement par torrent avec web seeds).",
        "categories": "2000,5000",
    },
]


def get_catalog_entry(catalog_id: str) -> Optional[Dict[str, Any]]:
    """Catalogue entry by id (case-insensitive), or None."""
    cid = (catalog_id or "").strip().lower()
    for entry in CATALOG:
        if entry["id"] == cid:
            return dict(entry)
    return None
