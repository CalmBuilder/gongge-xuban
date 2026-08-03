from zoneinfo import TZPATH, ZoneInfo, ZoneInfoNotFoundError, reset_tzpath


def test_iana_timezone_available_without_system_database() -> None:
    ZoneInfo.clear_cache()
    reset_tzpath(())
    try:
        timezone = ZoneInfo("Asia/Shanghai")
        assert timezone.key == "Asia/Shanghai"
    except ZoneInfoNotFoundError as exc:
        raise AssertionError("The tzdata package must provide IANA timezones on Windows") from exc
    finally:
        ZoneInfo.clear_cache()
        reset_tzpath(TZPATH)
