"""A starting server config generated from installed backend plugins."""

from collections.abc import Sequence

from wcpan.drive.plugin import factory_path, find_plugin, get_settings, render_settings

from .backend import BACKEND_PLUGIN_GROUP
from .config import CONFIG_VERSION


_SERVER_SECTION = """\
server:
  # SQLite database path.
  database_url:  # required
  # HTTP bind address.
  # host: 127.0.0.1
  # HTTP port.
  # port: 8080
  # Log file path for wcpan.logging (default: stderr only).
  # log_path: null
  # Directory for temporary upload files (default: system temp dir).
  # upload_tmp_dir: null"""


def create_config_scaffold(plugins: Sequence[str]) -> str:
    """A commented config with one instance and one mount per backend plugin.

    Each of *plugins* names an installed backend by entry point name, module, or
    factory path. Required settings are left empty to fill in.

    Raises:
        ValueError: If a plugin is not installed, ambiguous, or given twice.
    """
    if not plugins:
        raise ValueError("no backend plugin given")
    selected = [find_plugin([BACKEND_PLUGIN_GROUP], _) for _ in plugins]
    names = [_.name for _ in selected]
    if len(set(names)) != len(names):
        raise ValueError(f"backend plugins given twice: {names}")

    lines = [f"version: {CONFIG_VERSION}", "", _SERVER_SECTION, "", "backends:"]
    for ep in selected:
        lines.append(f"  {ep.name}:")
        lines.append(f"    type: {factory_path(ep)}")
        lines.extend(render_settings(get_settings(ep.load()), indent=4))
    lines.extend(["", "# Each mount appears as /<name>.", "mounts:"])
    for ep in selected:
        lines.append(f"  {ep.name}:")
        lines.append(f"    backend: {ep.name}")
        lines.append("    # Interpreted by the backend, such as a source directory.")
        lines.append("    source:  # required")
    return "\n".join(lines) + "\n"
