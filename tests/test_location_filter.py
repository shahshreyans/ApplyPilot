import pytest

from applypilot.discovery.location import (
    LocationFilter,
    load_location_filter,
    location_from_workday_url,
    location_ok,
    looks_like_us,
)

CANADA_CFG = {
    "country": "Canada",
    "location": {
        "accept_patterns": ["Regina", "Saskatchewan", "Canada"],
        "reject_patterns": ["India"],
    },
}


@pytest.mark.parametrize("loc", [
    "Austin, TX",
    "Austin, TX, US",
    "San Francisco, CA",
    "New York, NY (Remote)",
    "United States (Remote)",
    "Remote - USA",
    "Remote, US",
    "US Remote",
    "Texas, USA",
    "Seattle, Washington",
])
def test_us_locations_rejected(loc):
    assert looks_like_us(loc)
    assert not location_ok(loc, load_location_filter(CANADA_CFG))


@pytest.mark.parametrize("loc", [
    "Regina, SK",
    "Toronto, Ontario, Canada",
    "Remote",
    "Canada (Remote)",
    "Remote - Canada",
    None,
])
def test_canadian_and_generic_remote_accepted(loc):
    assert location_ok(loc, load_location_filter(CANADA_CFG))


def test_us_beats_accept_pattern():
    assert not location_ok("Remote - US or Canada", load_location_filter(CANADA_CFG))


NO_REMOTE_CFG = {**CANADA_CFG, "location": {**CANADA_CFG["location"], "exclude_remote": True}}


@pytest.mark.parametrize("loc", [
    "Remote",
    "Anywhere",
    "Canada (Remote)",
    "Remote - Canada",
    "Regina, SK (Remote)",
    "Work from home",
])
def test_exclude_remote_drops_all_remote(loc):
    assert not location_ok(loc, load_location_filter(NO_REMOTE_CFG))


@pytest.mark.parametrize("loc", ["Regina, SK", "Regina, Saskatchewan, Canada"])
def test_exclude_remote_keeps_onsite(loc):
    assert location_ok(loc, load_location_filter(NO_REMOTE_CFG))


def test_reject_patterns_win():
    assert not location_ok("Bangalore, India (Remote)", load_location_filter(CANADA_CFG))


def test_exclude_us_defaults_off_for_us_search():
    filt = load_location_filter({"country": "USA"})
    assert not filt.exclude_us
    assert location_ok("Remote - USA", filt)


def test_exclude_us_explicit_override():
    filt = load_location_filter({"country": "Canada", "location": {"exclude_us": False}})
    assert location_ok("United States (Remote)", filt)


def test_legacy_keys_still_read():
    filt = load_location_filter({"location_accept": ["Regina"], "location_reject_non_remote": ["Moose Jaw"]})
    assert filt.accept == ["Regina"] and filt.reject == ["Moose Jaw"]


def test_non_remote_unknown_city_rejected():
    assert not location_ok("London, UK", LocationFilter(accept=["Canada"]))


@pytest.mark.parametrize("loc", ["Toronto, ON, CA", "Vancouver, BC", "Regina, Saskatchewan, Canada", "Remote"])
def test_canadian_locations_not_flagged_as_us(loc):
    assert not looks_like_us(loc)


@pytest.mark.parametrize("loc", [
    "United States of America Eagan Minnesota",
    "Cambridge Massachusetts",
    "United States of America New York New York",
])
def test_us_locations_from_workday_paths(loc):
    assert looks_like_us(loc)


@pytest.mark.parametrize("loc", ["Canada Toronto Ontario", "SASKATOON Saskatchewan Canada", "Warsaw, Poland"])
def test_non_us_workday_paths(loc):
    assert not looks_like_us(loc)


@pytest.mark.parametrize("url,expected", [
    ("https://x.wd5.myworkdayjobs.com/Site/job/United-States-of-America-Eagan-Minnesota/Eng_JR1",
     "United States of America Eagan Minnesota"),
    ("https://x.wd1.myworkdayjobs.com/M_tx/job/Warsaw---Poland/Software-Engineer_R19072-1", "Warsaw, Poland"),
    ("/job/Regina-SK/Rep_2619178", "Regina SK"),
    ("https://x.wd3.myworkdayjobs.com/Experienced-Jobs/job/Lead-Engineers_JR00128860", None),
    (None, None),
])
def test_location_from_workday_url(url, expected):
    assert location_from_workday_url(url) == expected


def test_patterns_match_whole_words():
    filt = LocationFilter(accept=["SK"])
    assert location_ok("Regina, SK", filt)
    assert location_ok("Regina SK", filt)
    assert not location_ok("Poland, Gdansk", filt)
    assert not location_ok("Denmark, Roskilde", filt)


def test_exclude_unknown():
    assert location_ok(None, LocationFilter())
    assert not location_ok(None, LocationFilter(exclude_unknown=True))
    assert not location_ok("  ", LocationFilter(exclude_unknown=True))
