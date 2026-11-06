import pytest

from firm_runtime.errors import FirmError
from firm_runtime.notify import OwnerNotifier, script, validate_owner


def notifier(**kwargs):
    now = [0.0]
    posted = []
    sender = OwnerNotifier(runner=posted.append, clock=lambda: now[0], **kwargs)
    return sender, posted, now


def test_text_becomes_one_escaped_applescript_line():
    line = script('Say "hi"', 'a\\b "quoted"\nnext\tline')
    assert line == (
        'display notification "a\\\\b \\"quoted\\" next line" with title "Say \\"hi\\""'
    )
    assert len(script("t", "x" * 1000)) < 260


def test_one_notification_per_key_per_ten_minutes():
    sender, posted, now = notifier(platform="darwin")
    assert sender.notify_owner("T", "one", key=("e1", "mail"))
    assert not sender.notify_owner("T", "two", key=("e1", "mail"))
    assert sender.notify_owner("T", "other kind", key=("e1", "request"))
    assert sender.notify_owner("T", "other role", key=("e2", "mail"))
    now[0] += 600
    assert sender.notify_owner("T", "later", key=("e1", "mail"))
    assert [argv[:2] for argv in posted] == [["osascript", "-e"]] * 4


def test_nothing_is_posted_off_macos_or_when_the_owner_turned_it_off():
    sender, posted, _ = notifier(platform="linux")
    assert not sender.notify_owner("T", "B", key=("e1", "mail"))
    sender, posted, _ = notifier(platform="darwin")
    assert not sender.notify_owner("T", "B", key=("e1", "mail"), enabled=False)
    assert posted == []


def test_a_failing_runner_never_raises():
    def broken(argv):
        raise OSError("no osascript")

    sender = OwnerNotifier(runner=broken, platform="darwin")
    assert sender.notify_owner("T", "B", key=("e1", "mail")) is False


@pytest.mark.parametrize(
    "owner", [{"notifications": "no"}, {"notifications": True, "email": "x"}, "off"]
)
def test_owner_settings_are_validated(owner):
    with pytest.raises(FirmError):
        validate_owner({"owner": owner})
    validate_owner({"owner": {"notifications": False}})
    validate_owner({})
