# Security policy

If the repository host has private security advisories enabled, use that channel. If it does not, do not put sensitive material in an Issue or Discussion; wait for maintainers to publish a private reporting channel. Public Issues may contain only sanitized, non-sensitive defects.

Never attach provider configuration, expected identities, account fingerprints or their key, tokens, cookies, keyrings, manifests, filenames, addresses, host details, or migration logs. Revoke exposed credentials before reporting.

Only versions explicitly listed in a published release notice are supported. Python support is `>=3.11,<3.14`; Proton CLI support is limited to the exact configured build whose required interfaces pass that toolkit release's probe. There is no blanket Proton CLI support claim.

Expected account values remain in protected ignored configuration and are supplied to identity helpers through stdin, never process argv. Account fingerprints are domain-separated HMAC-SHA-256 values generated with a separate protected key file. The installer-generated bundle checksum verifies files copied from the local source tree; it does not authenticate a release, publisher, tag, or repository history.
