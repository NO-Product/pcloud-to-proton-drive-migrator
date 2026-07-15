# Evidence contract

Inventory and SHA1 enrichment precede freeze. Every later artifact binds one complete tuple: toolkit release and bundle SHA256; Python and rclone versions; exact Proton version/build and compatibility result; snapshot ID, digest algorithm, digest, generation, and frozen UTC time; and expected HMAC-keyed source/destination account fingerprints. Release-documented Proton interfaces are proven by the account probe and later filesystem/upload phases, not by a configurable identifier. Matching only the snapshot ID is insufficient.

Completion requires local and destination `missing`, `unexpected`, `type`, `size`, `sha1`, and `mtime` counts at zero; zero duplicate/unreadable blockers; accepted upload units/directories/files/bytes equal expected; complete destination directories/files/bytes; zero exhausted/error/auth/quota states; successful foreground controller phases; absent `ATTENTION_REQUIRED.json`; and an atomic durable handoff report with a satisfied gate.

Upload `complete`, 100 percent, or zero failed upload units is acceptance only.

`reports/completion-gate-latest.json` is written for every gate attempt and indexes the frozen manifest, local audit, upload acceptance/database, destination verification database, and event log. It is not completion evidence when `verified` is false. Only a satisfied gate writes `reports/final-handoff.json`, containing run ID; the complete tuple; source/local/SHA1 counts; upload expected/accepted counts and remediation; destination counts/classes/blockers; quota; controller results; terminal status; exact predicates; evidence index; limitations; and UTC completion time. It contains no raw identities, key material, credentials, or host data.

Proton SHA1 is client-claimed revision metadata, not server-side plaintext rehash. Original pCloud creation time cannot be set through the Proton CLI.
