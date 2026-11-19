"""Provider pins must be present in the distributed guest recipe."""

from importlib import resources

import yaml


def test_packaged_guest_recipe_contains_vendor_verified_agy_pin():
    recipe = resources.files("firm_sandbox").joinpath("assets/linux-base.yaml").read_text()
    script = yaml.safe_load(recipe)["provision"][0]["script"]
    assert "1.5.11-6132547114106880/linux-arm/cli_linux_arm64.tar.gz" in script
    assert (
        "AGY_SHA512=5f703c88e01851061c87c07b1a4ca54c1f5a6826ffada9fb005974a099e9a552"
        "ddb1f9ba2fe1194e2829803482c86084babf8cf91530a528bc7f659613600585"
    ) in script
    assert "sha512sum --check --status" in script
    assert "export AGY_CLI_DISABLE_AUTO_UPDATE=true" in script
    assert 'install -o root -g root -m 0755 "$agy_stage/antigravity"' in script
    assert "/usr/local/lib/node_modules/@anthropic-ai/claude-code/install.cjs" in script
    assert "/usr/local/bin/claude --version" in script
    assert "/usr/local/bin/codex --version" in script
    assert "/usr/local/bin/agy --version" in script
    assert resources.files("firm_sandbox").joinpath("assets/linux-generalize.sh").is_file()


def test_packaged_guest_recipe_installs_browsers_where_the_controller_links_them():
    from firm_sandbox import linux_vm_guest

    recipe = resources.files("firm_sandbox").joinpath("assets/linux-base.yaml").read_text()
    script = yaml.safe_load(recipe)["provision"][0]["script"]
    assert f"PLAYWRIGHT_BROWSERS_PATH={linux_vm_guest.SHARED_BROWSERS}" in script
    assert "test -s /opt/firm/playwright-version" in script
