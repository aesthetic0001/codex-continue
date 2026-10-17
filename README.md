# codex-continue

`codex-continue` launches the Codex CLI and watches its output for usage limit
reset times. When the limit resets, it sends a prompt to continue the session.

## Install with pipx

From a local checkout:

```sh
pipx install .
```

Or install directly from a Git repository:

```sh
pipx install 'git+https://github.com/<owner>/<repository>.git'
```

The Codex CLI must also be installed and available as `codex` on your `PATH`.

## Usage

```sh
codex-continue [--continue-prompt TEXT] [--] [CODEX_ARGS...]
```

The default continuation prompt is `/goal resume`. For example:

```sh
codex-continue --continue-prompt '/goal resume' -- --full-auto
```

Arguments after `--` are passed to `codex`.
