"""
redibis.store.config_store
===========================
Local-file-based store for reusable PII regex configs and quality rule
configs.  Configs are saved as YAML files in a local directory.

Configs are **NOT** part of data contracts — they are separate artifacts
that can be loaded into a run to customize behavior.

Usage
-----
    from redibis.store.config_store import LocalConfigStore
    from redibis.pii.regex_overrides import RegexOverrides

    store = LocalConfigStore("./configs")

    # Save a regex config
    overrides = RegexOverrides(add={"my_pattern": {...}})
    store.save_regex_config("egypt-strict", overrides)

    # List saved configs
    configs = store.list_regex_configs()  # [{"name": "egypt-strict", ...}]

    # Load and use in a scan
    loaded = store.load_regex_config("egypt-strict")
    detect_pii(df, regex_overrides=loaded)

Future
------
    A MinIO-backed ConfigStore will be added in Phase 2, reading from
    a dedicated bucket (separate from pii-contracts).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

log = logging.getLogger(__name__)


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Lock → bak → temp → replace for local JSON config files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    lock_path = path.with_suffix(path.suffix + ".lock")
    with open(lock_path, "a+", encoding="utf-8") as lock_fh:
        try:
            import fcntl
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        except Exception:
            pass
        try:
            if path.exists():
                shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
            fd, tmp_name = tempfile.mkstemp(
                dir=str(path.parent), prefix=".global_settings.", suffix=".tmp",
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                    tmp.write(text)
                    tmp.flush()
                    os.fsync(tmp.fileno())
                os.replace(tmp_name, path)
            except Exception:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
        finally:
            try:
                import fcntl
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass


class LocalConfigStore:
    """
    Local-file-based configuration store.

    Directory structure::

        {config_dir}/
            regex/
                egypt-strict.yaml
                telecom-v1.yaml
            quality/
                telecom-quality-v1.yaml
    """

    def __init__(self, config_dir: str | Path = Path("./configs")) -> None:
        self.config_dir = Path(config_dir)
        self.regex_dir = self.config_dir / "regex"
        self.quality_dir = self.config_dir / "quality"

    # ── Regex configs ─────────────────────────────────────────────────────

    def save_regex_config(
        self,
        name: str,
        overrides: "RegexOverrides",
        description: str = "",
    ) -> Path:
        """
        Save regex overrides to a local YAML file.

        Parameters
        ----------
        name : str
            Config name (used as filename, without extension).
        overrides : RegexOverrides
            The overrides to save.
        description : str
            Optional human-readable description.

        Returns
        -------
        Path — the file path written.
        """
        from redibis.pii.regex_overrides import RegexOverrides

        self.regex_dir.mkdir(parents=True, exist_ok=True)
        path = self.regex_dir / f"{_sanitize_name(name)}.yaml"

        data = {
            "name": name,
            "description": description,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "config": overrides.to_dict(),
        }

        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                data, f,
                default_flow_style=False,
                sort_keys=False,
                allow_unicode=True,
            )

        log.info("Saved regex config '%s' → %s", name, path)
        return path.resolve()

    def load_regex_config(self, name: str) -> "RegexOverrides":
        """
        Load a previously saved regex config by name.

        Raises FileNotFoundError if the config does not exist.
        """
        from redibis.pii.regex_overrides import RegexOverrides

        path = self.regex_dir / f"{_sanitize_name(name)}.yaml"
        if not path.exists():
            raise FileNotFoundError(
                f"Regex config '{name}' not found at {path}"
            )

        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        return RegexOverrides.from_dict(data.get("config", data))

    def list_regex_configs(self) -> list[dict]:
        """
        List all saved regex configs with metadata.

        Returns
        -------
        list[dict] — each dict has ``name``, ``description``,
        ``created_at``, ``file_path``, ``pattern_count``.
        """
        if not self.regex_dir.exists():
            return []

        configs = []
        for path in sorted(self.regex_dir.glob("*.yaml")):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                config = data.get("config", {})
                add = config.get("add") or {}
                remove = config.get("remove") or []
                replace_all = bool(config.get("replace_all", False))
                uses_builtin = not replace_all and not add and not remove
                pattern_count = len(add)
                catalog_pattern_count = 0
                if uses_builtin:
                    from redibis.pii.regex_catalog import list_catalog

                    catalog_pattern_count = len(list_catalog(active_only=True))
                    pattern_count = catalog_pattern_count
                configs.append({
                    "name": data.get("name", path.stem),
                    "description": data.get("description", ""),
                    "created_at": data.get("created_at", ""),
                    "file_path": str(path.resolve()),
                    "pattern_count": pattern_count,
                    "replace_all": replace_all,
                    "uses_builtin_catalog": uses_builtin,
                    "catalog_pattern_count": catalog_pattern_count,
                })
            except Exception as e:
                log.warning("Failed to read config %s: %s", path, e)
        return configs

    def delete_regex_config(self, name: str) -> bool:
        """
        Delete a saved regex config. Returns True if deleted, False if not found.
        """
        path = self.regex_dir / f"{_sanitize_name(name)}.yaml"
        if path.exists():
            path.unlink()
            log.info("Deleted regex config '%s'", name)
            return True
        return False

    # ── Quality configs ───────────────────────────────────────────────────

    def save_quality_config(
        self,
        name: str,
        rules: list[dict],
        description: str = "",
        ge_code: str = "",
    ) -> Path:
        """
        Save quality rule selections to a local YAML file.

        Parameters
        ----------
        name : str
            Config name.
        rules : list[dict]
            Quality rule definitions (from the interactive review).
        description : str
            Optional description.

        Returns
        -------
        Path — the file path written.
        """
        self.quality_dir.mkdir(parents=True, exist_ok=True)
        path = self.quality_dir / f"{_sanitize_name(name)}.yaml"

        data = {
            "name": name,
            "description": description,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "rule_count": len(rules),
            "rules": rules,
        }
        if ge_code:
            data["ge_code"] = ge_code

        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                data, f,
                default_flow_style=False,
                sort_keys=False,
                allow_unicode=True,
            )

        log.info("Saved quality config '%s' → %s (%d rules)", name, path, len(rules))
        return path.resolve()

    def load_quality_config(self, name: str) -> list[dict]:
        """Load previously saved quality rules by name."""
        return self.load_quality_config_doc(name).get("rules", [])

    def load_quality_config_doc(self, name: str) -> dict:
        """Load the full quality config document (rules + optional ge_code)."""
        path = self.quality_dir / f"{_sanitize_name(name)}.yaml"
        if not path.exists():
            raise FileNotFoundError(
                f"Quality config '{name}' not found at {path}"
            )

        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        return data

    def list_quality_configs(self) -> list[dict]:
        """List all saved quality configs with metadata."""
        if not self.quality_dir.exists():
            return []

        configs = []
        for path in sorted(self.quality_dir.glob("*.yaml")):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                configs.append({
                    "name": data.get("name", path.stem),
                    "description": data.get("description", ""),
                    "created_at": data.get("created_at", ""),
                    "file_path": str(path.resolve()),
                    "rule_count": data.get("rule_count", 0),
                })
            except Exception as e:
                log.warning("Failed to read config %s: %s", path, e)
        return configs

    def delete_quality_config(self, name: str) -> bool:
        """Delete a saved quality config. Returns True if deleted."""
        path = self.quality_dir / f"{_sanitize_name(name)}.yaml"
        if path.exists():
            path.unlink()
            log.info("Deleted quality config '%s'", name)
            return True
        return False

    # ── Global Settings ───────────────────────────────────────────────────

    def save_global_settings(self, settings: dict) -> str:
        """Save global app / UI preferences to a local JSON file (atomic)."""
        path = self.config_dir / "global_settings.json"
        _atomic_write_json(path, settings)
        return str(path.resolve())

    def load_global_settings(self) -> dict:
        """Load global app / UI preferences (empty dict if none saved)."""
        path = self.config_dir / "global_settings.json"
        if not path.exists():
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _sanitize_name(name: str) -> str:
    """Sanitize a config name for use as a filename/key."""
    import re
    # Replace spaces and special chars with hyphens, lowercase
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "-", name.strip())
    sanitized = re.sub(r"-+", "-", sanitized).strip("-")
    return sanitized.lower() or "unnamed"


# ─────────────────────────────────────────────────────────────────────────────
# Object Storage implementation (MinIO / SeaweedFS)
# ─────────────────────────────────────────────────────────────────────────────

class ObjectConfigStore:
    """
    S3/Object storage configuration store.
    Uses StorageBackend to manage configs in distinct buckets.

    Bucket layout::
        pii-configs/
            egypt-strict.yaml
            telecom-v1.yaml
        quality-configs/
            telecom-quality-v1.yaml
    """

    def __init__(
        self,
        backend: "StorageBackend",
        pii_bucket: str = "pii-configs",
        quality_bucket: str = "quality-configs",
    ) -> None:
        self.backend = backend
        self.pii_bucket = pii_bucket
        self.quality_bucket = quality_bucket

    # ── Regex configs ─────────────────────────────────────────────────────

    def save_regex_config(
        self,
        name: str,
        overrides: "RegexOverrides",
        description: str = "",
    ) -> str:
        """Save regex overrides to the PII configs bucket."""
        from redibis.pii.regex_overrides import RegexOverrides

        key = f"{_sanitize_name(name)}.yaml"
        data = {
            "name": name,
            "description": description,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "config": overrides.to_dict(),
        }

        self.backend.put_yaml(self.pii_bucket, key, data)
        log.info("Saved regex config '%s' to s3://%s/%s", name, self.pii_bucket, key)
        return f"s3://{self.pii_bucket}/{key}"

    def load_regex_config(self, name: str) -> "RegexOverrides":
        """Load a previously saved regex config by name."""
        from redibis.pii.regex_overrides import RegexOverrides

        key = f"{_sanitize_name(name)}.yaml"
        try:
            data = self.backend.get_yaml(self.pii_bucket, key)
            return RegexOverrides.from_dict(data.get("config", data))
        except KeyError:
            raise FileNotFoundError(f"Regex config '{name}' not found at s3://{self.pii_bucket}/{key}")

    def list_regex_configs(self) -> list[dict]:
        """List all saved regex configs with metadata."""
        configs = []
        try:
            keys = self.backend.list_keys(self.pii_bucket)
        except Exception:
            # If bucket doesn't exist yet, backend.list_keys might fail or return []
            keys = []

        for key in keys:
            if not key.endswith(".yaml"):
                continue
            try:
                data = self.backend.get_yaml(self.pii_bucket, key)
                config = data.get("config", {})
                add = config.get("add") or {}
                remove = config.get("remove") or []
                replace_all = bool(config.get("replace_all", False))
                uses_builtin = not replace_all and not add and not remove
                pattern_count = len(add)
                catalog_pattern_count = 0
                if uses_builtin:
                    from redibis.pii.regex_catalog import list_catalog

                    catalog_pattern_count = len(list_catalog(active_only=True))
                    pattern_count = catalog_pattern_count
                configs.append({
                    "name": data.get("name", key.removesuffix(".yaml")),
                    "description": data.get("description", ""),
                    "created_at": data.get("created_at", ""),
                    "file_path": f"s3://{self.pii_bucket}/{key}",
                    "pattern_count": pattern_count,
                    "replace_all": replace_all,
                    "uses_builtin_catalog": uses_builtin,
                    "catalog_pattern_count": catalog_pattern_count,
                })
            except Exception as e:
                log.warning("Failed to read config s3://%s/%s: %s", self.pii_bucket, key, e)
        return configs

    def delete_regex_config(self, name: str) -> bool:
        """Delete a saved regex config."""
        key = f"{_sanitize_name(name)}.yaml"
        if self.backend.exists(self.pii_bucket, key):
            self.backend.delete(self.pii_bucket, key)
            log.info("Deleted regex config s3://%s/%s", self.pii_bucket, key)
            return True
        return False

    # ── Quality configs ───────────────────────────────────────────────────

    def save_quality_config(
        self,
        name: str,
        rules: list[dict],
        description: str = "",
        ge_code: str = "",
    ) -> str:
        """Save quality rule selections to the quality configs bucket."""
        key = f"{_sanitize_name(name)}.yaml"
        data = {
            "name": name,
            "description": description,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "rule_count": len(rules),
            "rules": rules,
        }
        if ge_code:
            data["ge_code"] = ge_code

        self.backend.put_yaml(self.quality_bucket, key, data)
        log.info("Saved quality config '%s' to s3://%s/%s (%d rules)", name, self.quality_bucket, key, len(rules))
        return f"s3://{self.quality_bucket}/{key}"

    def load_quality_config(self, name: str) -> list[dict]:
        """Load previously saved quality rules by name."""
        return self.load_quality_config_doc(name).get("rules", [])

    def load_quality_config_doc(self, name: str) -> dict:
        """Load the full quality config document (rules + optional ge_code)."""
        key = f"{_sanitize_name(name)}.yaml"
        try:
            data = self.backend.get_yaml(self.quality_bucket, key)
            return data or {}
        except KeyError:
            raise FileNotFoundError(
                f"Quality config '{name}' not found at s3://{self.quality_bucket}/{key}"
            )

    def list_quality_configs(self) -> list[dict]:
        """List all saved quality configs with metadata."""
        configs = []
        try:
            keys = self.backend.list_keys(self.quality_bucket)
        except Exception:
            keys = []

        for key in keys:
            if not key.endswith(".yaml"):
                continue
            try:
                data = self.backend.get_yaml(self.quality_bucket, key)
                configs.append({
                    "name": data.get("name", key.removesuffix(".yaml")),
                    "description": data.get("description", ""),
                    "created_at": data.get("created_at", ""),
                    "file_path": f"s3://{self.quality_bucket}/{key}",
                    "rule_count": data.get("rule_count", 0),
                })
            except Exception as e:
                log.warning("Failed to read config s3://%s/%s: %s", self.quality_bucket, key, e)
        return configs

    def delete_quality_config(self, name: str) -> bool:
        """Delete a saved quality config."""
        key = f"{_sanitize_name(name)}.yaml"
        if self.backend.exists(self.quality_bucket, key):
            self.backend.delete(self.quality_bucket, key)
            log.info("Deleted quality config s3://%s/%s", self.quality_bucket, key)
            return True
        return False

    # ── Global Settings ───────────────────────────────────────────────────

    def save_global_settings(self, settings: dict) -> str:
        """Save global app settings to the quality bucket."""
        key = "global_settings.json"
        self.backend.put_json(self.quality_bucket, key, settings)
        return f"s3://{self.quality_bucket}/{key}"

    def load_global_settings(self) -> dict:
        """Load global app settings."""
        key = "global_settings.json"
        try:
            return self.backend.get_json(self.quality_bucket, key)
        except KeyError:
            return {}
