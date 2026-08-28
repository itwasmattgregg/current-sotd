"""Canned MusicBrainz payloads shaped like the real web service responses.

The live API is not reachable from CI, so the tests replay these instead. The
live bootleg deliberately puts the song at track 8 -- that is the number the
old tagger kept inheriting.
"""

SEARCH_RESPONSE = {
    "count": 2,
    "recordings": [
        {
            "id": "rec-live",
            "score": 100,
            "title": "Skinny Love",
            "length": 251000,
            "artist-credit": [
                {
                    "name": "Bon Iver",
                    "joinphrase": "",
                    "artist": {
                        "id": "art-1",
                        "name": "Bon Iver",
                        "sort-name": "Bon Iver",
                    },
                }
            ],
        },
        {
            "id": "rec-studio",
            "score": 98,
            "title": "Skinny Love",
            "length": 238866,
            "artist-credit": [
                {
                    "name": "Bon Iver",
                    "joinphrase": "",
                    "artist": {
                        "id": "art-1",
                        "name": "Bon Iver",
                        "sort-name": "Bon Iver",
                    },
                }
            ],
        },
    ],
}

_STUDIO_RELEASE = {
    "id": "rel-album",
    "title": "For Emma, Forever Ago",
    "status": "Official",
    "date": "2008-02-19",
    "country": "US",
    "release-group": {
        "id": "rg-1",
        "title": "For Emma, Forever Ago",
        "primary-type": "Album",
        "secondary-types": [],
        "first-release-date": "2007-07-08",
    },
    "media": [
        {
            "position": 1,
            "format": "CD",
            "track-count": 9,
            "track-offset": 2,
            "track": [
                {
                    "id": "trk-3",
                    "number": "3",
                    "title": "Skinny Love",
                    "length": 238866,
                }
            ],
        }
    ],
}

_BOOTLEG_RELEASE = {
    "id": "rel-live",
    "title": "Live at the Cedar 2009-03-01",
    "status": "Bootleg",
    "date": "2009-03-01",
    "release-group": {
        "id": "rg-2",
        "title": "Live at the Cedar",
        "primary-type": "Album",
        "secondary-types": ["Live"],
        "first-release-date": "2009-03-01",
    },
    "media": [
        {
            "position": 1,
            "track-count": 15,
            "track-offset": 7,
            "track": [{"id": "trk-8", "number": "8", "title": "Skinny Love"}],
        }
    ],
}

RECORDING_STUDIO = {
    "id": "rec-studio",
    "title": "Skinny Love",
    "length": 238866,
    "isrcs": ["USJAY0700018"],
    "artist-credit": [
        {
            "name": "Bon Iver",
            "joinphrase": "",
            "artist": {"id": "art-1", "name": "Bon Iver", "sort-name": "Bon Iver"},
        }
    ],
    "genres": [
        {"name": "folk", "count": 2},
        {"name": "indie folk", "count": 7},
    ],
    "relations": [
        {"type": "performance", "work": {"id": "work-1", "title": "Skinny Love"}}
    ],
    "releases": [_BOOTLEG_RELEASE, _STUDIO_RELEASE],
}

RECORDING_LIVE = {
    "id": "rec-live",
    "title": "Skinny Love",
    "length": 251000,
    "artist-credit": [
        {
            "name": "Bon Iver",
            "joinphrase": "",
            "artist": {"id": "art-1", "name": "Bon Iver", "sort-name": "Bon Iver"},
        }
    ],
    "releases": [_BOOTLEG_RELEASE],
}

RELEASE_DETAIL = {
    "id": "rel-album",
    "title": "For Emma, Forever Ago",
    "status": "Official",
    "date": "2008-02-19",
    "country": "US",
    "barcode": "656605213729",
    "artist-credit": [
        {
            "name": "Bon Iver",
            "joinphrase": "",
            "artist": {"id": "art-1", "name": "Bon Iver", "sort-name": "Bon Iver"},
        }
    ],
    "label-info": [
        {"catalog-number": "JAG115", "label": {"id": "lbl-1", "name": "Jagjaguwar"}}
    ],
    "release-group": {
        "id": "rg-1",
        "title": "For Emma, Forever Ago",
        "primary-type": "Album",
        "secondary-types": [],
        "first-release-date": "2007-07-08",
    },
    "media": [{"position": 1, "format": "CD", "track-count": 9}],
}

WORK_DETAIL = {
    "id": "work-1",
    "title": "Skinny Love",
    "relations": [
        {"type": "composer", "artist": {"id": "art-1", "name": "Justin Vernon"}},
        {"type": "lyricist", "artist": {"id": "art-1", "name": "Justin Vernon"}},
        {"type": "publishing", "artist": {"id": "art-9", "name": "Some Publisher"}},
    ],
}

RELEASE_GROUP_DETAIL = {
    "id": "rg-1",
    "title": "For Emma, Forever Ago",
    "primary-type": "Album",
    "first-release-date": "2007-07-08",
    "genres": [{"name": "indie folk", "count": 12}],
}
