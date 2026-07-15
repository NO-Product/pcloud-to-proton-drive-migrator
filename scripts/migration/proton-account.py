#!/usr/bin/env python3
"""Safely identify the authenticated Proton account without exposing identity."""

import argparse
import json
import subprocess
import sys

from migration_common import (
    ACCOUNT_FINGERPRINT_ALGORITHM, ClassifiedError, atomic_json, classify_error, now,
    read_expected_account_stdin, read_fingerprint_key_file, verify_expected_account,
)


IDENTITY_KEYS = {
    "accountemail": "account_email", "email": "account_email", "emailaddress": "account_email",
    "accountid": "account_id", "userid": "account_id", "useridentifier": "account_id",
    "username": "username",
}
QUOTA_KEYS = {
    "quota": "quota_bytes", "quotabytes": "quota_bytes", "maxspace": "quota_bytes",
    "usedquota": "used_bytes", "usedbytes": "used_bytes", "usedspace": "used_bytes",
}


def _key(value):
    return "".join(character for character in str(value).lower() if character.isalnum())


def _walk(value):
    if isinstance(value, dict):
        if value.get("ok") is True and "value" in value:
            yield from _walk(value["value"])
        for key, child in value.items():
            yield key, child
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def account_facts(payload):
    identities = {}
    quota = {}
    for key, value in _walk(payload):
        normalized_key = _key(key)
        unwrapped = value
        while isinstance(unwrapped, dict) and unwrapped.get("ok") is True and "value" in unwrapped:
            unwrapped = unwrapped["value"]
        if normalized_key in IDENTITY_KEYS and not isinstance(unwrapped, (dict, list)) and unwrapped is not None:
            identities.setdefault(IDENTITY_KEYS[normalized_key], set()).add(str(unwrapped).strip())
        if normalized_key in QUOTA_KEYS and not isinstance(unwrapped, (dict, list)):
            try:
                quota[QUOTA_KEYS[normalized_key]] = int(unwrapped)
            except (TypeError, ValueError):
                pass
    ambiguous = [key for key, values in identities.items() if len(values) > 1]
    if ambiguous:
        raise ClassifiedError("ambiguity", "Proton account info contains ambiguous stable identity fields")
    return {key: next(iter(values)) for key, values in identities.items() if values}, quota


def proton_version(args):
    try:
        result = subprocess.run(
            [args.proton_run, args.proton_bin, "--version"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=args.timeout,
        )
    except Exception as error:
        raise ClassifiedError(classify_error(str(error)), "Proton version inspection failed") from error
    version = result.stdout.decode("utf-8", "replace").strip()
    if result.returncode or not version:
        raise ClassifiedError(classify_error(version), "Proton version inspection failed")
    return version


def inspect_account(args, version):
    try:
        result = subprocess.run(
            [args.proton_run, args.proton_bin, "account", "info", "-j"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=args.timeout,
        )
    except Exception as error:
        raise ClassifiedError(classify_error(str(error)), "Proton account inspection failed") from error
    text = result.stdout.decode("utf-8", "replace")
    if result.returncode:
        raise ClassifiedError(classify_error(text), "Proton account inspection failed")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise ClassifiedError("invalid-response", "Proton account info returned invalid JSON") from error
    identity, quota = account_facts(payload)
    fingerprint = verify_expected_account("proton", args.expected_account, identity, args.fingerprint_key)
    return {"kind": "proton_account", "status": "verified", "generated_at": now(),
            "destination_account_fingerprint": fingerprint,
            "fingerprint_algorithm": ACCOUNT_FINGERPRINT_ALGORITHM,
            "proton_version": version, "version_compatible": True,
            "quota": {"quota_bytes": quota.get("quota_bytes"), "used_bytes": quota.get("used_bytes")}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--proton-run", required=True)
    parser.add_argument("--proton-bin", required=True)
    parser.add_argument("--expected-account-stdin", action="store_true", required=True)
    parser.add_argument("--fingerprint-key-file", required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--report")
    args = parser.parse_args()
    try:
        args.expected_account = read_expected_account_stdin(sys.stdin)
        args.fingerprint_key = read_fingerprint_key_file(args.fingerprint_key_file)
        version = proton_version(args)
        if version != args.expected_version:
            result = {"kind": "proton_account", "status": "failed-version-compatibility",
                      "generated_at": now(), "proton_version": version, "version_compatible": False,
                      "destination_account_fingerprint": None,
                      "quota": {"quota_bytes": None, "used_bytes": None},
                      "error_class": "version-mismatch", "error": "Proton version does not match --expected-version",
                      "attention_required": True}
            code = 2
        else:
            result = inspect_account(args, version)
            code = 0
    except ClassifiedError as error:
        status = "blocked-authentication" if error.error_class == "authentication" else "failed-account-verification"
        result = {"kind": "proton_account", "status": status, "generated_at": now(),
                  "proton_version": None, "version_compatible": False,
                  "destination_account_fingerprint": None,
                  "quota": {"quota_bytes": None, "used_bytes": None},
                  "error_class": error.error_class, "error": str(error), "attention_required": True}
        code = 2
    if args.report:
        atomic_json(args.report, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
