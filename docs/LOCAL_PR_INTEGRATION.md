# Local PR integration — 2026-11-09

These upstream changes were selectively merged into the customized local build.
The existing PS4 target validation, fresh baseline checks, function-part guards,
scratch compiler previews, TLS support and compiler profiles are retained.

| PR | Imported head | Behavior |
| --- | --- | --- |
| [#182](https://github.com/n-delic/decompdecomp/pull/182) | `53da4a332dbbda3b7d953755933c16b01b807c32` | Instruction differences after accounting for registers and scheduling |
| [#178](https://github.com/n-delic/decompdecomp/pull/178) | `221a3a02c16249417ad9a4d2edbe215f9f386cec` | Build-failure tracing and visible-definition judging |
| [#179](https://github.com/n-delic/decompdecomp/pull/179) | `cedda687375a95052a721b0b00a26dde27e588b8` | C++ permutation and turn-scoped restoration builds |
| [#180](https://github.com/n-delic/decompdecomp/pull/180) | `8460b942e269794f9dab37c14b474a7dc8d91376` | Unit-build batching, process-tree timeout cleanup and configure inventories |
| [#244](https://github.com/n-delic/decompdecomp/pull/244) | `98afeda0834fcdecaad2f8edac44bd732d6afe5d` | Bounded manual deep permutation with results UI |
| [#202](https://github.com/n-delic/decompdecomp/pull/202) | `a1d0e845fe35ca73fe29d8128afb01634d3e7b7d` | Responsive startup and cancellable helper resumption |

The configure inventory improvement also applies to the local PS4 template.
Existing projects keep their own generated build tools until explicitly refreshed.
The live app and game projects were not restarted or changed during integration.

The local judge continues to rebuild fresh baselines; the upstream timestamp-only
baseline shortcut was not substituted for independent local validation. Trial runs
defer restoration builds while preserving that requirement.

PRs #241 and #242 are outside this integration: the former targets ROM settings
search, and the latter had outstanding review concerns when this integration began.

## Validation

- Full regression: 1,008 passed, 216 skipped, one known system-GTK test excluded.
- Real pinned generator and synthetic compiler checks: 78 passed, including C++
  variants, deep-search budgets, PC/PS4 configure inventories and PS4 call matching.
- Final startup checks: 5 passed, including exactly one background autosave start,
  responsiveness during helper resumption and cancellation/shutdown guards.
- Corrected a legacy jump-table fixture to declare its actual original address;
  all five jump-table tests pass with the installed pinned Clang.
- Ruff, changed JavaScript syntax and whitespace checks pass.

No application environment was rebuilt, no live database was written and no game
project was reconfigured. The app needs a normal restart to load all Python changes.
