# Zed MoonBit LSP Sample

Open this folder in Zed, then open `cmd/main/main.mbt` and check:

- Hover on `println`
- Completion after `@sample.`

To check diagnostics manually, temporarily add an invalid call inside `fn main`:

```moonbit
let x = @sample.greeting()
```

Expected diagnostics:

- `E4080`: `greeting` requires one `String` argument.
- `E0002`: `x` is unused.

The repository-level LSP smoke test performs this diagnostics check in memory,
so this sample stays valid on disk and should pass:

```sh
moon check
```
