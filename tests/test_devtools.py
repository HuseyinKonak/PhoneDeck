import pytest

from phonedeck.core import devtools
from phonedeck.core.adb import AdbError
from phonedeck.core.device import SEPARATOR
from phonedeck.core.devtools import (
    Entry,
    ForwardRule,
    IntentSpec,
    WmValue,
    build_intent_command,
)

INJECTIONS = ("a; rm -rf /", "a b", "a$(id)", "a`id`", "a|b", "", "a\nb", "a'b")


class FakeAdb:
    """Records shell and adb calls; replies with canned output."""

    def __init__(self, shell_output: str = "", run_output: str = "") -> None:
        self.shell_output = shell_output
        self.run_output = run_output
        self.shell_calls: list[str] = []
        self.run_calls: list[list[str]] = []

    def shell(self, serial: str, command: str, timeout: float = 30) -> str:
        self.shell_calls.append(command)
        return self.shell_output

    def run(self, args: list[str], serial: str | None = None, timeout: float = 30) -> str:
        self.run_calls.append(args)
        return self.run_output


def joined(*sections: str) -> str:
    return f"\n{SEPARATOR}\n".join(sections)


# --- validators ---------------------------------------------------------------
@pytest.mark.parametrize("key", ["font_scale", "debug.layout", "A1_b.c"])
def test_validate_key_accepts_plain_keys(key):
    assert devtools.validate_key(key) == key


@pytest.mark.parametrize("key", INJECTIONS + ("a-b", "ü"))
def test_validate_key_rejects_shell_metacharacters(key):
    with pytest.raises(ValueError):
        devtools.validate_key(key)


def test_validate_namespace_rejects_unknown_and_injection():
    with pytest.raises(ValueError):
        devtools.validate_namespace("global; reboot")
    with pytest.raises(ValueError):
        devtools.validate_namespace("")
    assert devtools.validate_namespace("secure") == "secure"


@pytest.mark.parametrize("text", ["1080x2400", " 720x1600 "])
def test_validate_size_accepts_width_by_height(text):
    assert devtools.validate_size(text) == text.strip()


@pytest.mark.parametrize("text", ["1080", "1080x", "x2400", "1080x2400; reboot", "10x10", "99999x99999", "1080X2400", ""])
def test_validate_size_rejects_bad_input(text):
    with pytest.raises(ValueError):
        devtools.validate_size(text)


def test_validate_density_enforces_range():
    assert devtools.validate_density("420") == 420
    for bad in ("0", "71", "1201", "abc", "420; id", ""):
        with pytest.raises(ValueError):
            devtools.validate_density(bad)


def test_validate_port_enforces_range():
    assert devtools.validate_port(8080) == 8080
    for bad in ("0", "65536", "-1", "80a", "tcp:80", ""):
        with pytest.raises(ValueError):
            devtools.validate_port(bad)


def test_validate_timeout_enforces_range():
    assert devtools.validate_timeout("30000") == 30_000
    for bad in ("999", "86400001", "1s"):
        with pytest.raises(ValueError):
            devtools.validate_timeout(bad)


def test_validate_scale_formats_and_bounds():
    assert devtools.validate_scale(1.0, 0.5, 3.0, "x") == "1"
    assert devtools.validate_scale("1.15", 0.5, 3.0, "x") == "1.15"
    for bad in ("nan", "4", "0.1", "1; id", "", "inf"):
        with pytest.raises(ValueError):
            devtools.validate_scale(bad, 0.5, 3.0, "x")


def test_validate_spec_rejects_injection():
    assert devtools.validate_spec("tcp:8080") == "tcp:8080"
    with pytest.raises(ValueError):
        devtools.validate_spec("tcp:80; reboot")


# --- parsers --------------------------------------------------------------------
def test_parse_wm_reads_physical_and_override():
    value = devtools.parse_wm("Physical size: 1080x2400\nOverride size: 720x1600\n")

    assert value == WmValue("1080x2400", "720x1600")
    assert value.current == "720x1600"


def test_parse_wm_without_override_uses_physical():
    value = devtools.parse_wm("Physical density: 420")

    assert (value.physical, value.override, value.current) == ("420", "", "420")


def test_parse_wm_handles_garbage():
    assert devtools.parse_wm("error") == WmValue()


def test_parse_night_mode():
    assert devtools.parse_night_mode("Night mode: yes") == "yes"
    assert devtools.parse_night_mode("Night mode: AUTO") == "auto"
    assert devtools.parse_night_mode("") == ""


def test_parse_numbers_treat_null_and_junk_as_missing():
    assert devtools.parse_float("null") is None
    assert devtools.parse_float("1.15\n") == 1.15
    assert devtools.parse_int("30000") == 30_000
    assert devtools.parse_int("oops") is None


def test_parse_properties_sorts_and_skips_noise():
    output = "[ro.b]: [two]\n[ro.a]: []\nnot a property\n"

    assert devtools.parse_properties(output) == (Entry("ro.a", ""), Entry("ro.b", "two"))


def test_parse_settings_list_joins_multiline_values():
    output = "b=2\na=first\nsecond line\nc=\n"

    assert devtools.parse_settings_list(output) == (
        Entry("a", "first\nsecond line"),
        Entry("b", "2"),
        Entry("c", ""),
    )


def test_parse_settings_list_keeps_equals_signs_in_values():
    assert devtools.parse_settings_list("url=a=b") == (Entry("url", "a=b"),)


def test_parse_rules_filters_forwards_by_serial():
    output = "abc tcp:8080 tcp:80\nother tcp:9 tcp:9\n\n"

    rules = devtools.parse_rules(output, "forward", "abc")

    assert rules == (ForwardRule("forward", "tcp:8080", "tcp:80"),)


def test_parse_rules_reverse_uses_last_two_columns():
    rules = devtools.parse_rules("UsbFfs tcp:8081 tcp:8081", "reverse")

    assert rules == (ForwardRule("reverse", "tcp:8081", "tcp:8081"),)


def test_parse_extras_text_splits_on_first_equals():
    extras = devtools.parse_extras_text("a.b=1\n\nurl=x=y\n")

    assert extras == (("a.b", "1"), ("url", "x=y"))


@pytest.mark.parametrize("text", ["novalue", "bad key=1", "k;id=1", "=1"])
def test_parse_extras_text_rejects_bad_lines(text):
    with pytest.raises(ValueError):
        devtools.parse_extras_text(text)


# --- command builders -------------------------------------------------------------
def test_put_setting_command_quotes_values():
    command = devtools.put_setting_command("system", "font_scale", "1; reboot")

    assert command == "settings put system font_scale '1; reboot'"


def test_put_setting_command_rejects_bad_key_or_namespace():
    with pytest.raises(ValueError):
        devtools.put_setting_command("system", "k; reboot", "1")
    with pytest.raises(ValueError):
        devtools.put_setting_command("proc", "k", "1")


def test_delete_setting_command_validates():
    assert devtools.delete_setting_command("global", "k") == "settings delete global k"
    with pytest.raises(ValueError):
        devtools.delete_setting_command("global", "k && id")


def test_layout_bounds_command_sets_property_and_pokes_activity():
    assert devtools.layout_bounds_command(True) == (
        "setprop debug.layout true; service call activity 1599295570"
    )
    assert "false" in devtools.layout_bounds_command(False)


def test_build_intent_command_full():
    spec = IntentSpec(
        kind="broadcast",
        action="com.example.PING",
        data="https://a.b/?q=1&r=2",
        component="com.example/.Receiver",
        categories=("android.intent.category.DEFAULT",),
        extras=(("note", "hello world"),),
    )

    assert build_intent_command(spec) == (
        "am broadcast -a com.example.PING -d 'https://a.b/?q=1&r=2' "
        "-n com.example/.Receiver -c android.intent.category.DEFAULT --es note 'hello world'"
    )


def test_build_intent_command_quotes_hostile_data_and_extras():
    spec = IntentSpec(data="x'; reboot; '", extras=(("k", "$(id)"),))

    command = build_intent_command(spec)

    assert "-d 'x'\"'\"'; reboot; '\"'\"''" in command
    assert "--es k '$(id)'" in command


def test_build_intent_command_requires_a_target():
    with pytest.raises(ValueError):
        build_intent_command(IntentSpec())


@pytest.mark.parametrize("field", ["action", "component"])
def test_build_intent_command_rejects_injection_in_identifiers(field):
    with pytest.raises(ValueError):
        build_intent_command(IntentSpec(**{field: "a; reboot", "data": "x"}))


def test_build_intent_command_rejects_bad_category_and_extra_key():
    with pytest.raises(ValueError):
        build_intent_command(IntentSpec(action="a", categories=("c; id",)))
    with pytest.raises(ValueError):
        build_intent_command(IntentSpec(action="a", extras=(("k;id", "v"),)))


def test_build_intent_command_rejects_unknown_kind():
    with pytest.raises(ValueError):
        build_intent_command(IntentSpec(kind="service", action="a"))


# --- device calls --------------------------------------------------------------------
def test_read_display_parses_every_section():
    sections = [
        "Physical size: 1080x2400\nOverride size: 720x1600",
        "Physical density: 420",
        "Night mode: yes",
        "1.15",
        "30000",
        "7",
    ]
    fake = FakeAdb(joined(*sections))

    state = devtools.read_display(fake, "s")

    assert state.size.current == "720x1600"
    assert state.density == WmValue("420", "")
    assert (state.night_mode, state.font_scale) == ("yes", 1.15)
    assert (state.screen_timeout_ms, state.stay_awake) == (30_000, True)


def test_read_display_tolerates_null_settings():
    fake = FakeAdb(joined("", "", "", "null", "null", "null"))

    state = devtools.read_display(fake, "s")

    assert state.font_scale is None and state.screen_timeout_ms is None
    assert state.stay_awake is False


def test_read_display_raises_on_incomplete_output():
    with pytest.raises(AdbError):
        devtools.read_display(FakeAdb("only one"), "s")


def test_read_debug_parses_flags_and_scales():
    fake = FakeAdb(joined("1", "0", "true", "1.0", "0.5", "null"))

    state = devtools.read_debug(fake, "s")

    assert (state.show_touches, state.pointer_location, state.layout_bounds) == (True, False, True)
    assert state.animation_scales == (1.0, 0.5, None)


def test_set_size_and_density_build_wm_commands():
    fake = FakeAdb()

    devtools.set_size(fake, "s", "1080x2400")
    devtools.set_density(fake, "s", "420")
    devtools.reset_size(fake, "s")
    devtools.reset_density(fake, "s")

    assert fake.shell_calls == [
        "wm size 1080x2400", "wm density 420", "wm size reset", "wm density reset",
    ]


def test_set_size_rejects_injection_without_running_anything():
    fake = FakeAdb()

    with pytest.raises(ValueError):
        devtools.set_size(fake, "s", "1080x2400; reboot")

    assert fake.shell_calls == []


def test_set_dark_mode_uses_uimode():
    fake = FakeAdb()

    devtools.set_dark_mode(fake, "s", True)
    devtools.set_dark_mode(fake, "s", False)

    assert fake.shell_calls == ["cmd uimode night yes", "cmd uimode night no"]


def test_set_font_scale_timeout_and_stay_awake_commands():
    fake = FakeAdb()

    devtools.set_font_scale(fake, "s", 1.3)
    devtools.set_screen_timeout(fake, "s", 60_000)
    devtools.set_stay_awake(fake, "s", True)
    devtools.set_stay_awake(fake, "s", False)

    assert fake.shell_calls == [
        "settings put system font_scale 1.3",
        "settings put system screen_off_timeout 60000",
        "settings put global stay_on_while_plugged_in 7",
        "settings put global stay_on_while_plugged_in 0",
    ]


def test_set_font_scale_rejects_out_of_range():
    with pytest.raises(ValueError):
        devtools.set_font_scale(FakeAdb(), "s", 9)


def test_set_debug_flag_writes_system_setting():
    fake = FakeAdb()

    devtools.set_debug_flag(fake, "s", "show_touches", True)

    assert fake.shell_calls == ["settings put system show_touches 1"]


def test_set_debug_flag_rejects_injected_key():
    with pytest.raises(ValueError):
        devtools.set_debug_flag(FakeAdb(), "s", "show_touches 1; reboot #", True)


def test_set_animation_scales_sets_all_three_in_one_call():
    fake = FakeAdb()

    devtools.set_animation_scales(fake, "s", 0.5)

    assert len(fake.shell_calls) == 1
    for name in devtools.ANIMATION_SCALES:
        assert f"settings put global {name} 0.5" in fake.shell_calls[0]


def test_set_animation_scales_rejects_unknown_name_and_bad_value():
    with pytest.raises(ValueError):
        devtools.set_animation_scales(FakeAdb(), "s", 1, names=("evil; id",))
    with pytest.raises(ValueError):
        devtools.set_animation_scales(FakeAdb(), "s", "x")


def test_shell_errors_become_adb_errors():
    fake = FakeAdb("Error: bad size\nusage...")

    with pytest.raises(AdbError, match="bad size"):
        devtools.set_size(fake, "s", "1080x2400")


def test_power_runs_the_matching_adb_command():
    fake = FakeAdb()

    for key in ("reboot", "recovery", "bootloader", "poweroff"):
        devtools.power(fake, "s", key)

    assert fake.run_calls == [
        ["reboot"], ["reboot", "recovery"], ["reboot", "bootloader"], ["shell", "reboot -p"],
    ]


def test_power_rejects_unknown_action():
    fake = FakeAdb()

    with pytest.raises(ValueError):
        devtools.power(fake, "s", "reboot; id")

    assert fake.run_calls == []


def test_add_rule_forward_and_reverse_order_endpoints():
    fake = FakeAdb()

    devtools.add_rule(fake, "s", "forward", "8080", "80")
    devtools.add_rule(fake, "s", "reverse", 3000, 3001)

    assert fake.run_calls == [
        ["forward", "tcp:8080", "tcp:80"],
        ["reverse", "tcp:3001", "tcp:3000"],
    ]


def test_add_rule_rejects_bad_port_and_kind():
    fake = FakeAdb()

    with pytest.raises(ValueError):
        devtools.add_rule(fake, "s", "forward", "0", "80")
    with pytest.raises(ValueError):
        devtools.add_rule(fake, "s", "forward", "80; id", "80")
    with pytest.raises(ValueError):
        devtools.add_rule(fake, "s", "shell", "80", "80")
    assert fake.run_calls == []


def test_remove_rule_uses_first_endpoint():
    fake = FakeAdb()

    devtools.remove_rule(fake, "s", ForwardRule("forward", "tcp:8080", "tcp:80"))
    devtools.remove_rule(fake, "s", ForwardRule("reverse", "tcp:3001", "tcp:3000"))

    assert fake.run_calls == [
        ["forward", "--remove", "tcp:8080"],
        ["reverse", "--remove", "tcp:3001"],
    ]


def test_remove_rule_rejects_hostile_rule():
    with pytest.raises(ValueError):
        devtools.remove_rule(FakeAdb(), "s", ForwardRule("forward", "tcp:1; id", "tcp:2"))
    with pytest.raises(ValueError):
        devtools.remove_rule(FakeAdb(), "s", ForwardRule("shell", "tcp:1", "tcp:2"))


def test_list_rules_combines_forward_and_reverse():
    class TwoLists(FakeAdb):
        def run(self, args, serial=None, timeout=30):
            self.run_calls.append(args)
            return "s tcp:1 tcp:2" if args[0] == "forward" else "UsbFfs tcp:3 tcp:4"

    rules = devtools.list_rules(TwoLists(), "s")

    assert rules == (ForwardRule("forward", "tcp:1", "tcp:2"), ForwardRule("reverse", "tcp:3", "tcp:4"))


def test_read_settings_lists_namespace_and_rejects_bad_one():
    fake = FakeAdb("a=1\nb=2")

    entries = devtools.read_settings(fake, "s", "global")

    assert fake.shell_calls == ["settings list global"]
    assert entries == (Entry("a", "1"), Entry("b", "2"))
    with pytest.raises(ValueError):
        devtools.read_settings(fake, "s", "global; id")


def test_read_properties_runs_getprop():
    fake = FakeAdb("[ro.x]: [1]")

    assert devtools.read_properties(fake, "s") == (Entry("ro.x", "1"),)
    assert fake.shell_calls == ["getprop"]


def test_put_setting_quotes_value_and_rejects_empty():
    fake = FakeAdb()

    devtools.put_setting(fake, "s", "secure", "some.key", "a b; id")

    assert fake.shell_calls == ["settings put secure some.key 'a b; id'"]
    with pytest.raises(ValueError):
        devtools.put_setting(fake, "s", "secure", "some.key", "  ")


def test_delete_setting_validates_key():
    fake = FakeAdb()

    devtools.delete_setting(fake, "s", "system", "k")

    assert fake.shell_calls == ["settings delete system k"]
    with pytest.raises(ValueError):
        devtools.delete_setting(fake, "s", "system", "k; reboot")


def test_run_intent_returns_trimmed_output():
    fake = FakeAdb("Starting: Intent { act=x }\n")

    output = devtools.run_intent(fake, "s", IntentSpec(action="x"))

    assert output == "Starting: Intent { act=x }"
    assert fake.shell_calls == ["am start -a x"]


def test_intent_summary_names_kind_and_target():
    assert IntentSpec(kind="broadcast", action="a.b").summary() == "Yayın: a.b"
    assert IntentSpec(component="p/.C", action="a").summary() == "Etkinlik: p/.C"
