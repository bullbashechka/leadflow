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

## Mobile app design

[Approved mobile concept](crm-mobile-app-approved.png) was generated with the built-in imagegen tool and approved by the user on 2026-10-05. The design brief was an app-style Leadflow CRM with a floating rounded bottom control panel, existing product capabilities, and three screens: list, read-only details, and manual creation. Its data is illustrative.

The earlier implementation previews above remain historical records of the desktop redesign. Current mobile previews use explicitly labeled browser-test fixtures:

- [Lead list](crm-mobile-app-list-preview.png)
- [Lead details](crm-mobile-app-detail-preview.png)
- [Creation form](crm-mobile-app-form-preview.png)
- [Menu](crm-mobile-app-menu-preview.png)

Requirements live in [PRD](../../PRD.md#список-лидов). Technical choices and verification results live in [TASKS](../../TASKS.md#срез-ui-мобильная-crm-в-стиле-приложения).

To regenerate the mobile previews, run from the repository root:

```sh
python3 scripts/test_auth_browser.py --grep 'mobile app: approved layout previews' --artifacts /tmp/leadflow-mobile-preview
```

Copy `phone-mobile-app-list-390.png`, `phone-mobile-app-detail-390.png`, `phone-mobile-app-form-390.png`, and `phone-mobile-app-menu-390.png` from the printed screenshot directory to the corresponding preview filenames above. Remove your temporary artifact directory after copying and inspecting the images. These files are review artifacts; the application does not import them.
