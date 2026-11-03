from importlib.resources import files


def test_operator_templates_ship_with_the_framework_service():
    templates = files("chatcore_release_control").joinpath("templates")
    for name in (
        "p04-ci.yml.inactive",
        "p08-runtime-compose.yml.template",
        "p09-runner-relay-job.yml.template",
        "p10-watchdog.timer.template",
    ):
        assert templates.joinpath(name).is_file(), name
