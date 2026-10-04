# Repository protection

The canonical repository is [AIowa-LLC/lumen](https://github.com/AIowa-LLC/lumen).

`main` uses an active GitHub ruleset: changes go through pull requests, the
**Validation** check must pass against the latest base, review conversations must
be resolved, and force pushes and branch deletion are blocked. The ruleset has no
bypass actors. Merge using squash or rebase to preserve linear history.

The repository currently has one maintainer, so pull requests do not require a
second person's approval. This allows the maintainer to merge their own work once
checks pass. Add an approval requirement when additional reviewers are available.

CI runs lint and the unit/FFmpeg integration suite in a pinned Arch container with
GTK bindings, matching the supported Omarchy multimedia stack.
It uses a read-only workflow token and a pinned checkout action with credentials
removed. It does not capture the desktop or run opt-in live verification scripts.
Omarchy capture and native playback still need the documented local checks.

Dependabot checks Python and GitHub Actions dependencies weekly. Dependency
alerts, automatic security fixes, and secret-scanning push protection are enabled
in the repository settings. Do not put recordings, secrets, or local runtimes in
commits. See [Development and contributing](DEVELOPMENT.md).
