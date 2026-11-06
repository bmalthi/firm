# Source and attribution

Firm's code, prompts, examples and documentation are hosted at
[bmalthi/firm](https://github.com/bmalthi/firm) and use the root
[Apache-2.0 license](../LICENSE). Keep [NOTICE](../NOTICE) with distributions.
Each Python package contains copies of these files so its source archive can build
independently; release checks require the copies to match the root files.

The owner uses **bmalthi** as the public copyright and contributor identity.

Python dependencies, uv, Lima and provider CLIs remain separate dependencies with
their own licenses. Package wheels contain Firm's source and assets, not bundled
copies of these dependencies. macOS, Xcode and other applications installed into
owner-created VM images retain their own terms. Firm's license does not grant
redistribution rights to those images or applications.

When adding copied code or assets, identify their source, check the license and
preserve applicable notices before including them in a release. This inventory
records identified sources; it is not a legal opinion or a claim to own third-party
material.

The `launchctl-*.txt` test fixtures preserve macOS command-output structure for
parser tests. Session socket paths, temporary paths, timestamps and UUIDs are
synthetic; Apple service labels and executable paths describe the platform.
