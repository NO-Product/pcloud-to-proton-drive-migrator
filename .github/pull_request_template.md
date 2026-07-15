## Change

Describe the operational guarantee and recovery behavior added or changed.

## Safety checklist

- [ ] pCloud remains read-only.
- [ ] Proton recovery is non-destructive.
- [ ] Runtime state and credentials are excluded.
- [ ] Checkpoints and evidence are preserved.
- [ ] Static contracts cover the change.
- [ ] Managed systemd phase work stays in the foreground.
- [ ] Upload acceptance remains distinct from completion.
- [ ] Compatibility versions/interfaces and account fingerprints are recorded without identities.
