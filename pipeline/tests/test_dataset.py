def test_simc_metadata_carries_the_game_build_the_reader_needs():
    """ "Is last night's tuning in here?" is answered by the hotfix date on the page.

    The game build is not at the report's top level -- it is under the first actor's
    dbc block, keyed by data source -- so a naive top-level read misses it.
    """
    from wowdps.simc_runner import simc_metadata

    report = {
        "version": "1210-01",
        "ptr_enabled": False,
        "sim": {
            "players": [
                {
                    "dbc": {
                        "Live": {
                            "build_level": 69273,
                            "wow_version": "12.1.0.69273",
                            "hotfix_date": "2026-08-12",
                        }
                    }
                }
            ]
        },
    }
    meta = simc_metadata(report)
    assert meta["wowVersion"] == "12.1.0.69273"
    assert meta["wowBuild"] == 69273
    assert meta["hotfixDate"] == "2026-08-12"


def test_a_report_without_a_dbc_block_leaves_the_game_build_null_not_absent():
    """Null tells a reader "no build in this report"; absent looks like a bug."""
    from wowdps.simc_runner import simc_metadata

    meta = simc_metadata({"version": "1210-01", "sim": {"players": []}})
    assert meta["wowVersion"] is None
    assert meta["hotfixDate"] is None
