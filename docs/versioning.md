# Versioning & stability policy

- **SemVer.** Breaking API changes only in major releases (post-1.0); minor
  releases add features behind off-by-default policy fields; patches fix bugs.
- **Frozen codes.** `Code` enum values and `ValidationResult.to_dict()` keys
  are frozen once released. New failure classes get *new* codes; existing
  codes never change meaning or spelling.
- **Off-by-default.** Every new `Policy` field defaults to off/None: a policy
  object valid on version N validates identically on N+1.
- **Deprecations** live one minor release behind a `DeprecationWarning`
  before removal, with the replacement named in the warning.
- **Releasing** is `git tag vX.Y.Z && git push --tags`: CI builds, checks,
  publishes to PyPI via trusted publishing (OIDC), and cuts a GitHub release.
  Update `CHANGELOG.md` and `__version__` in the same PR as the tag.
