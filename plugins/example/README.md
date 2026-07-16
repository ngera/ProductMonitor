# Example plugin

A working plugin skeleton for [documents/PLUGIN_AUTHORS.md](../../documents/PLUGIN_AUTHORS.md).

`plugin.py` here fetches nothing real — it yields a small in-memory
sample of `RawItem` objects each time it runs. Purpose: prove the
manifest + registration + fetch loop works end-to-end so plugin
authors can copy this and iterate.

## To try it

1. Rename this directory (or copy it) so it becomes your plugin's
   real name. The default `example` directory is deliberately
   excluded from drop-in discovery to avoid polluting real installs.
2. Set the trust flag:
   ```powershell
   $env:TRUST_PLUGINS_DIR = ".\plugins"
   python -m webui.app
   ```
3. Visit `/connections` — you should see "Example Plugin" listed.

## What to modify for your real plugin

- Edit `plugin.py`:
  - Change `plugin_id`, `display_name`, `docs_url`, and `help` in the
    `MANIFEST`.
  - Adjust `connection_fields` (env vars needed) and `stream_fields`
    (per-stream config on the product's Sources page).
  - Replace `MySource._fetch_static_items()` with your real API
    integration.
- Add a test file at `tests/test_example.py` in your fork.
- Update this README with a real description.
