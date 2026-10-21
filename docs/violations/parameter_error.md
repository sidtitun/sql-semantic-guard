# parameter_error

Explicit driver binding could not create a complete SQL/parameters pair.
Supply all named placeholders through `validate(sql, params={...})`. Use named
input (`:name` or dialect-native named placeholders), not anonymous `?` or native
positional `$1` placeholders. Choose the desired output with `Policy.paramstyle`.

This error blocks in both enforcing and shadow mode. Invalid results expose
neither executable SQL nor parameter values. See [driver parameters](../PARAMETERS.md).
