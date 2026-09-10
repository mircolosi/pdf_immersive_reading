"""Load config.yaml (single dict, no schema class — spec has no need for one)."""
import yaml

# No default path: it used to be the relative "config.yaml", which silently
# resolved against the caller's cwd. Callers pass an absolute path built from
# their own __file__.


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)
