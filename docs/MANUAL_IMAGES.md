# Explicit manual manuscript image handoff

The `Blog API Prepare` workflow has an owner-only `manual-images` mode for the
single manually reviewed `owner-20261009-new-policy` packet. It is not a generic
quality-gate override or a new scheduled job. The code pins both the exact packet
bytes and body SHA-256. The original ID, reference date, manual review status,
existing daily usage, save receipts and text history remain unchanged.

## Input and execution boundaries

- Use only the existing approved private encryption route and `BLOG_BUNDLE_KEY`.
  Encrypt the exact reviewed UTF-8 JSON bytes with Fernet; do not reserialize them.
  Confirm a local decrypt/hash round trip without printing the key or manuscript.
- Select `mode=manual-images`, the exact `manual_image_request_id`, and supply only
  Fernet ciphertext in `manual_image_packet` (maximum 40,000 characters). Never
  put plaintext, photos, API keys, storage URLs or login details in workflow inputs.
- The runner restores the latest encrypted state before doing any work. It never
  bootstraps a fresh ledger, imports new candidates, calls a writer/reviewer,
  resets the day limit, refreshes the policy selection, or saves/publishes a post.
- The reviewed packet must still match the pinned hashes. Any edit, even a JSON
  formatting edit, requires reconciliation rather than automatic approval.
- Existing image model/key/configuration and image function are reused. The
  current short investment ceiling is at most three images, not a minimum.
  The two company explanation sections are selected from the pinned manuscript;
  existing thumbnail title/style handling remains unchanged.
- A prior STARTED, UNCERTAIN, FAILED, or missing-file READY outcome never triggers
  another paid submission in this mode, including after the normal recovery
  cooldown. The existing safe, bounded explicit-429 retry remains available.
  Successful cached images are reused; changed plans or checksums fail closed.
  This is an image-count/attempt boundary, not a dollar-denominated cost cap.

## Output and remaining review

The encrypted `blog-state-*` artifact retains the manual packet, generated files,
image-plan identities and usage manifests, including partial results. The manual
packet is kept across retention-by-reference-date pruning so an old pending call
cannot lose its paid checkpoint. There is no new public plaintext artifact.

`MANUAL_IMAGES_READY` means the image API files are available. The post itself
remains `LOCAL_TEXT_REVIEWED`, outside automatic `APPROVED`/`ready.json` delivery.
After decrypting through the existing authorized handoff, independently inspect
the actual images and text before any separately authorized Naver save. Neither
this mode nor its PR authorizes merge, paid dispatch, publication or a new secret.
