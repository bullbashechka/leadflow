# CRM visual references

The approved CRM redesign uses these files as visual references:

- [Approved desktop mockup](crm-approved.png): generated image approved for implementation.
- [Behance reference 1](behance-reference-1.png): screenshot supplied by the user.
- [Behance reference 2](behance-reference-2.png): screenshot supplied by the user.

The images guide visual styling. Names, contacts, requests, counts, dates, and labels in them are illustrative. They are not CRM records, seed data, or runtime assets. The implementation displays persisted application data and preserves existing tag names.

Product behavior is defined in [PRD.md](../../PRD.md#список-лидов). Technical UI decisions and verified delivery progress are maintained in [TASKS.md](../../TASKS.md#срез-ui-редизайн-crm).

## Implementation previews

- [Desktop](crm-desktop-preview.png)
- [Mobile](crm-mobile-preview.png)

These screenshots were captured from the running CRM with explicitly labeled browser-test fixtures. They contain no customer records. They document the verified implementation; the approved mockup above remains the visual reference.

To regenerate the previews, run the existing isolated browser runner:

```sh
python3 scripts/test_auth_browser.py --grep 'approved layout preview' --artifacts /tmp/leadflow-crm-preview
```

Copy `desktop-crm-preview-demo.png` and `phone-crm-preview-demo.png` from the printed screenshot directory to the preview filenames above. The runner setup is documented in [README.md](../../README.md#checks).
