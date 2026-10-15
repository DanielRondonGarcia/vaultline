from collections.abc import Mapping
from typing import Any


RETENTION_FIELDS = ("keep_last", "keep_hourly", "keep_daily", "keep_weekly", "keep_monthly", "keep_yearly")
RETENTION_ENVIRONMENT = {field: f"RESTIC_{field.upper()}" for field in RETENTION_FIELDS}
RETENTION_POLICY_CONFIGURED_ENVIRONMENT = "RESTIC_RETENTION_POLICY_CONFIGURED"
RESTIC_PRUNE_ENVIRONMENT = "RESTIC_PRUNE"
LEGACY_RETENTION_DEFAULTS = {
    "keep_last": None,
    "keep_hourly": None,
    "keep_daily": 7,
    "keep_weekly": 4,
    "keep_monthly": 12,
    "keep_yearly": 1,
}


def _policy_value(policy: Any, field: str) -> Any:
    if isinstance(policy, Mapping):
        return policy.get(field)
    return getattr(policy, field)


def _validated_policy_values(
    *,
    keep_last: Any,
    keep_hourly: Any,
    keep_daily: Any,
    keep_weekly: Any,
    keep_monthly: Any,
    keep_yearly: Any,
    prune: Any,
) -> dict[str, Any]:
    values = {
        "keep_last": keep_last,
        "keep_hourly": keep_hourly,
        "keep_daily": keep_daily,
        "keep_weekly": keep_weekly,
        "keep_monthly": keep_monthly,
        "keep_yearly": keep_yearly,
    }
    for field, value in values.items():
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError(f"Restic {field} retention must be a non-negative integer")
    if not any(value is not None for value in values.values()):
        raise ValueError("Restic retention requires at least one keep rule")
    if type(prune) is not bool:
        raise ValueError("Restic prune must be a boolean")
    return {**values, "prune": prune}


def build_restic_forget_command(
    *,
    keep_last: Any = None,
    keep_hourly: Any = None,
    keep_daily: Any = None,
    keep_weekly: Any = None,
    keep_monthly: Any = None,
    keep_yearly: Any = None,
    prune: Any = True,
) -> list[str]:
    values = _validated_policy_values(
        keep_last=keep_last,
        keep_hourly=keep_hourly,
        keep_daily=keep_daily,
        keep_weekly=keep_weekly,
        keep_monthly=keep_monthly,
        keep_yearly=keep_yearly,
        prune=prune,
    )
    command = ["restic", "forget"]
    for field in RETENTION_FIELDS:
        value = values[field]
        if value is not None:
            command.extend([f"--{field.replace('_', '-')}", str(value)])
    if values["prune"]:
        command.append("--prune")
    return command


def apply_target_retention_policy(environment: Mapping[str, Any], policy: Any) -> dict[str, Any]:
    values = _validated_policy_values(
        **{field: _policy_value(policy, field) for field in RETENTION_FIELDS},
        prune=_policy_value(policy, "prune"),
    )
    result = dict(environment)
    for field, variable in RETENTION_ENVIRONMENT.items():
        value = values[field]
        # Empty strings deliberately clear worker-level values for unset target rules.
        result[variable] = "" if value is None else str(value)
    result[RESTIC_PRUNE_ENVIRONMENT] = "true" if values["prune"] else "false"
    result[RETENTION_POLICY_CONFIGURED_ENVIRONMENT] = "true"
    return result


def restic_retention_config_from_environment(environment: Mapping[str, Any]) -> dict[str, Any]:
    configured_raw = environment.get(RETENTION_POLICY_CONFIGURED_ENVIRONMENT)
    if configured_raw is None:
        target_policy_configured = False
    elif isinstance(configured_raw, str) and configured_raw.strip().lower() in {"1", "true", "yes", "on"}:
        target_policy_configured = True
    elif isinstance(configured_raw, str) and configured_raw.strip().lower() in {"0", "false", "no", "off"}:
        target_policy_configured = False
    else:
        raise ValueError("RESTIC_RETENTION_POLICY_CONFIGURED must be a boolean")

    values: dict[str, Any] = {}
    for field in RETENTION_FIELDS:
        raw = environment.get(RETENTION_ENVIRONMENT[field])
        if raw is None or (target_policy_configured and raw == ""):
            value = None if target_policy_configured else LEGACY_RETENTION_DEFAULTS[field]
        else:
            if not isinstance(raw, str) or not raw.isascii() or not raw.isdigit():
                raise ValueError(f"{RETENTION_ENVIRONMENT[field]} must be a non-negative integer")
            value = int(raw)
        values[f"restic_{field}"] = value

    prune_raw = environment.get(RESTIC_PRUNE_ENVIRONMENT) if target_policy_configured else None
    if prune_raw is None:
        prune = True
    elif isinstance(prune_raw, str) and prune_raw.strip().lower() in {"1", "true", "yes", "on"}:
        prune = True
    elif isinstance(prune_raw, str) and prune_raw.strip().lower() in {"0", "false", "no", "off"}:
        prune = False
    else:
        raise ValueError("RESTIC_PRUNE must be a boolean")

    _validated_policy_values(
        **{field: values[f"restic_{field}"] for field in RETENTION_FIELDS},
        prune=prune,
    )
    values["restic_prune"] = prune
    return values
