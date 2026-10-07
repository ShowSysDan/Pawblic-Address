import json
import os

import pytest

import settings


def test_defaults_when_there_is_no_file():
    assert settings.load() == settings.DEFAULTS


def test_save_round_trip_reports_changes(settings_file):
    cfg, changes = settings.save({"ip": "10.0.0.5", "port": "7000", "codec": "MP3",
                                  "syslog_host": "logs.example.org", "junk": 1})
    assert cfg["port"] == 7000 and cfg["codec"] == "mp3"
    assert changes == {"ip": ("192.168.1.100", "10.0.0.5"), "port": (4848, 7000),
                       "codec": ("pcm", "mp3"), "syslog_host": ("", "logs.example.org")}
    assert settings.load() == cfg
    assert "junk" not in json.loads(settings_file.read_text())
    assert not os.path.exists(str(settings_file) + ".tmp")


def test_saving_the_same_values_reports_no_changes():
    settings.save({"port": 5000})
    assert settings.save({"port": 5000})[1] == {}


@pytest.mark.parametrize("ip", [
    "", "1.2.3.4/x", "a b", "rtp://x", "host?pkt_size=1", "user@host", "host#x",
    "-bad.example", "bad-.example", "999.1.1.1", "1.2.3", "x" * 64 + ".com", "host:99", "a..b",
])
def test_bad_hosts_are_rejected(ip):
    with pytest.raises(ValueError):
        settings.save({"ip": ip})


@pytest.mark.parametrize("ip,stored", [
    ("10.0.0.5", "10.0.0.5"), (" core.local ", "core.local"), ("q-sys-core", "q-sys-core"),
    ("fe80::1", "fe80::1"), ("2001:DB8::1", "2001:db8::1"),
])
def test_good_hosts_are_accepted(ip, stored):
    assert settings.save({"ip": ip})[0]["ip"] == stored


@pytest.mark.parametrize("bad", [
    {"port": 0}, {"port": 70000}, {"port": "x"}, {"port": True}, {"port": None},
    {"bitrate": 31}, {"bitrate": 321}, {"codec": "opus"},
    {"syslog_port": 0}, {"syslog_host": "not a host"},
])
def test_bad_values_are_rejected(bad):
    with pytest.raises(ValueError):
        settings.save(bad)


def test_nothing_is_written_when_any_field_is_invalid():
    settings.save({"port": 5000})
    with pytest.raises(ValueError):
        settings.save({"port": 6000, "bitrate": 9999})
    assert settings.load()["port"] == 5000


def test_syslog_can_be_turned_off_again():
    settings.save({"syslog_host": "10.1.1.1"})
    assert settings.save({"syslog_host": "  "})[0]["syslog_host"] == ""


def test_non_object_is_rejected():
    with pytest.raises(ValueError):
        settings.save([1, 2])


def test_corrupt_file_falls_back_to_defaults(settings_file):
    settings_file.write_text("{not json")
    assert settings.load() == settings.DEFAULTS


def test_bad_stored_values_fall_back_one_by_one(settings_file):
    # e.g. someone hand-edited settings.json: the bad key falls back, the good one stays.
    settings_file.write_text(json.dumps({"ip": "a b?x", "port": 5000, "codec": "flac"}))
    cfg = settings.load()
    assert cfg["ip"] == settings.DEFAULTS["ip"]
    assert cfg["codec"] == settings.DEFAULTS["codec"]
    assert cfg["port"] == 5000
