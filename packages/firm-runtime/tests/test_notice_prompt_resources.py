from firm_runtime.prompt_resources import prompt, render


def test_notice_resources_are_packaged_and_render_once():
    source = prompt("notices.json")
    assert "shared_capacity_wait_body" in source

    body = render(
        "notices",
        "shared_capacity_wait_body",
        address="founder/analyst",
        instruction="The owner must restore the shared service.",
    )
    assert body.count("founder/analyst") == 1
    assert body.count("The owner must restore the shared service.") == 1
    assert "${" not in body


def test_notice_values_are_literal_after_substitution():
    body = render(
        "notices",
        "owner_mandate_revision_body",
        revision=2,
        explanation="Use ${literal} and $5 exactly.",
    )
    assert body.count("Use ${literal} and $5 exactly.") == 1
