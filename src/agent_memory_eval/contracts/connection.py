"""Versioned connection-check profiles; historical v1.0 remains readable."""

from copy import deepcopy
import json

from jsonschema import Draft202012Validator

from agent_memory_eval.contracts.core import ContractError
from agent_memory_eval.contracts.validation import ROOT


def validate_profile(profile):
    schemas = {"1.0": "connection-check.schema.json", "1.1": "connection-check-v1.1.schema.json"}
    filename = schemas.get(profile.get("profile_version"))
    if filename is None:
        raise ContractError("Unsupported connection profile version")
    schema = json.loads((ROOT / "schemas" / filename).read_text())
    Draft202012Validator(schema).validate(profile)


def provider_route(profile, role):
    return profile[role]["provider"] if profile["profile_version"] == "1.1" else profile["provider"]


def forward_seed(profile, role):
    return profile[role]["forward_native_seed"] if profile["profile_version"] == "1.1" else True


def native_parameters(profile, role):
    # The pinned tau2 initialize() injects the benchmark seed into BOTH roles.
    # Validate that native injection even when a role explicitly omits it on wire.
    parameters = deepcopy(profile[role]["parameters"])
    parameters["seed"] = profile["benchmark"]["seed"]
    return parameters
