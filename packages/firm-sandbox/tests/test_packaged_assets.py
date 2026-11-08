"""Provider pins must be present in the distributed guest recipe."""

from importlib import resources

import yaml


def test_packaged_guest_recipe_contains_vendor_verified_agy_pin():
    recipe = resources.files("firm_sandbox").joinpath("assets/linux-vm.yaml").read_text()
    script = yaml.safe_load(recipe)["provision"][0]["script"]
    assert "AGY_VERSION=1.5.3" in script
    assert (
        "AGY_SHA512=c2fac818b3c1bb224079df03a714ccba99728b6a3a41d86d247ab2feb8582a438"
        "40c5032f1c2b75e72667623d5503e7ffda2712c4f37b6ee19f99c3fcfb25bae"
    ) in script
    assert "sha512sum --check --status" in script
    assert "export AGY_CLI_DISABLE_AUTO_UPDATE=true" in script
    assert 'install -o root -g root -m 0644 "$agy_stage/version"' in script
